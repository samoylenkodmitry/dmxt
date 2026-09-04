// Keep source-compatible with Ubuntu 24.04's Rust 1.75 toolchain.
#![allow(clippy::io_other_error, clippy::unnecessary_map_or)]

use std::{
    env,
    fs::{File, OpenOptions},
    io,
    io::Write,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};

use axum::{
    body::Body,
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Query, State,
    },
    http::{header, HeaderValue, Response, StatusCode},
    response::IntoResponse,
    routing::get,
    Json, Router,
};
use futures_util::StreamExt;
#[cfg(not(target_os = "linux"))]
use memchr::memmem;
use serde::{Deserialize, Serialize};
#[cfg(not(target_os = "linux"))]
use tokio::{io::AsyncReadExt, process::Command};
use tokio::{
    sync::watch,
    time::{sleep, timeout},
};
#[cfg(target_os = "linux")]
use v4l::{
    buffer::Type,
    io::{mmap::Stream as MmapStream, traits::CaptureStream},
    video::Capture,
    Device, Format, FourCC,
};

const KEYBOARD: &str = "/dev/hidg0";
const MOUSE: &str = "/dev/hidg1";
const POINTER: &str = "/dev/hidg2";

#[derive(Clone)]
struct Frame {
    sequence: u64,
    jpeg: Arc<Vec<u8>>,
    captured_ms: u64,
}

#[derive(Clone)]
struct AppState {
    frame_rx: watch::Receiver<Option<Frame>>,
    sequence: Arc<AtomicU64>,
    last_frame_ms: Arc<AtomicU64>,
    started: Instant,
    size: Arc<str>,
    fps: u32,
    hid: Arc<Hid>,
}

struct Hid {
    keyboard: Mutex<File>,
    mouse: Mutex<File>,
    pointer: Mutex<File>,
    reports: AtomicU64,
    last_write_us: AtomicU64,
    max_write_us: AtomicU64,
}

#[derive(Serialize)]
struct Health {
    ok: bool,
    implementation: &'static str,
    video: VideoHealth,
    input: InputHealth,
}

#[derive(Serialize)]
struct VideoHealth {
    ok: bool,
    size: String,
    fps: u32,
    sequence: u64,
    age_ms: Option<u64>,
}

#[derive(Serialize)]
struct InputHealth {
    reports: u64,
    last_write_us: u64,
    max_write_us: u64,
}

#[derive(Deserialize)]
struct SnapshotQuery {
    after: Option<u64>,
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> io::Result<()> {
    let device = env::var("DMXT_VIDEO_DEVICE").unwrap_or_else(|_| "/dev/video1".into());
    let size: Arc<str> = env::var("DMXT_VIDEO_SIZE")
        .unwrap_or_else(|_| "1280x720".into())
        .into();
    let fps = env::var("DMXT_VIDEO_FPS")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(60);
    let bind = env::var("DMXT_FAST_BIND").unwrap_or_else(|_| "127.0.0.1:8024".into());
    let hid = Arc::new(Hid::open()?);

    let (frame_tx, frame_rx) = watch::channel(None);
    let started = Instant::now();
    let sequence = Arc::new(AtomicU64::new(0));
    let last_frame_ms = Arc::new(AtomicU64::new(0));
    #[cfg(target_os = "linux")]
    std::thread::Builder::new()
        .name("dmxt-v4l2".into())
        .spawn({
            let size = size.clone();
            let sequence = sequence.clone();
            let last_frame_ms = last_frame_ms.clone();
            move || {
                capture_loop(
                    device,
                    size,
                    fps,
                    started,
                    frame_tx,
                    sequence,
                    last_frame_ms,
                )
            }
        })?;
    #[cfg(not(target_os = "linux"))]
    tokio::spawn(capture_loop(
        device,
        size.clone(),
        fps,
        started,
        frame_tx,
        sequence.clone(),
        last_frame_ms.clone(),
    ));

    let state = AppState {
        frame_rx,
        sequence,
        last_frame_ms,
        started,
        size,
        fps,
        hid,
    };
    let app = Router::new()
        .route("/health", get(health))
        .route("/snapshot", get(snapshot))
        .route("/video", get(video_upgrade))
        .route("/input", get(input_upgrade))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(&bind).await?;
    eprintln!("dmxt-fast listening on {bind}");
    axum::serve(listener, app).await
}

fn publish_frame(
    jpeg: Vec<u8>,
    started: Instant,
    frame_tx: &watch::Sender<Option<Frame>>,
    sequence: &AtomicU64,
    last_frame_ms: &AtomicU64,
) {
    let captured_ms = started.elapsed().as_millis() as u64;
    let sequence_value = sequence.fetch_add(1, Ordering::Relaxed) + 1;
    last_frame_ms.store(captured_ms, Ordering::Relaxed);
    frame_tx.send_replace(Some(Frame {
        sequence: sequence_value,
        jpeg: Arc::new(jpeg),
        captured_ms,
    }));
}

#[cfg(target_os = "linux")]
fn capture_loop(
    device: String,
    size: Arc<str>,
    fps: u32,
    started: Instant,
    frame_tx: watch::Sender<Option<Frame>>,
    sequence: Arc<AtomicU64>,
    last_frame_ms: Arc<AtomicU64>,
) {
    loop {
        if let Err(error) = capture_v4l2(
            &device,
            &size,
            fps,
            started,
            &frame_tx,
            &sequence,
            &last_frame_ms,
        ) {
            eprintln!("V4L2 capture failed: {error}");
        }
        std::thread::sleep(Duration::from_millis(500));
    }
}

#[cfg(target_os = "linux")]
fn capture_v4l2(
    device: &str,
    size: &str,
    fps: u32,
    started: Instant,
    frame_tx: &watch::Sender<Option<Frame>>,
    sequence: &AtomicU64,
    last_frame_ms: &AtomicU64,
) -> io::Result<()> {
    let (width, height) = size
        .split_once('x')
        .and_then(|(width, height)| Some((width.parse().ok()?, height.parse().ok()?)))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid video size"))?;
    let capture = Device::with_path(device)?;
    let actual = capture.set_format(&Format::new(width, height, FourCC::new(b"MJPG")))?;
    if actual.width != width || actual.height != height || actual.fourcc != FourCC::new(b"MJPG") {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            format!("capture returned {actual}"),
        ));
    }
    capture.set_params(&v4l::video::capture::Parameters::with_fps(fps))?;

    // This UVC bridge requires four queued MMAP buffers to begin streaming.
    // We dequeue continuously and publish through a latest-value channel, so
    // slow clients still cannot accumulate stale frames.
    let mut stream = MmapStream::with_buffers(&capture, Type::VideoCapture, 4)?;
    loop {
        let (buffer, metadata) = stream.next()?;
        let jpeg = &buffer[..metadata.bytesused as usize];
        if jpeg.len() >= 4 && jpeg.starts_with(b"\xff\xd8") && jpeg.ends_with(b"\xff\xd9") {
            publish_frame(jpeg.to_vec(), started, frame_tx, sequence, last_frame_ms);
        }
    }
}

#[cfg(not(target_os = "linux"))]
async fn capture_loop(
    device: String,
    size: Arc<str>,
    fps: u32,
    started: Instant,
    frame_tx: watch::Sender<Option<Frame>>,
    sequence: Arc<AtomicU64>,
    last_frame_ms: Arc<AtomicU64>,
) {
    loop {
        let mut child = match Command::new("/usr/bin/ffmpeg")
            .args([
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-analyzeduration",
                "0",
                "-probesize",
                "32",
                "-f",
                "v4l2",
                "-input_format",
                "mjpeg",
                "-video_size",
                &size,
                "-framerate",
                &fps.to_string(),
                "-i",
                &device,
                "-an",
                "-c:v",
                "copy",
                "-flush_packets",
                "1",
                "-f",
                "mjpeg",
                "pipe:1",
            ])
            .kill_on_drop(true)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .spawn()
        {
            Ok(child) => child,
            Err(error) => {
                eprintln!("capture start failed: {error}");
                sleep(Duration::from_millis(500)).await;
                continue;
            }
        };

        let mut stdout = child.stdout.take().expect("ffmpeg stdout");
        let mut chunk = vec![0_u8; 256 * 1024];
        let mut buffer = Vec::with_capacity(2 * 1024 * 1024);
        loop {
            let read = match stdout.read(&mut chunk).await {
                Ok(0) => break,
                Ok(read) => read,
                Err(error) => {
                    eprintln!("capture read failed: {error}");
                    break;
                }
            };
            buffer.extend_from_slice(&chunk[..read]);

            loop {
                let Some(start) = memmem::find(&buffer, b"\xff\xd8") else {
                    if buffer.len() > 1 {
                        buffer.drain(..buffer.len() - 1);
                    }
                    break;
                };
                let Some(relative_end) = memmem::find(&buffer[start + 2..], b"\xff\xd9") else {
                    if start > 0 {
                        buffer.drain(..start);
                    }
                    if buffer.len() > 8 * 1024 * 1024 {
                        buffer.clear();
                    }
                    break;
                };
                let end = start + 2 + relative_end + 2;
                let jpeg = buffer[start..end].to_vec();
                buffer.drain(..end);
                publish_frame(jpeg, started, &frame_tx, &sequence, &last_frame_ms);
            }
        }

        let _ = child.kill().await;
        sleep(Duration::from_millis(500)).await;
    }
}

async fn health(State(state): State<AppState>) -> Json<Health> {
    let last = state.last_frame_ms.load(Ordering::Relaxed);
    let age = (last != 0).then(|| state.started.elapsed().as_millis() as u64 - last);
    Json(Health {
        ok: age.is_some_and(|value| value < 2_000),
        implementation: "rust",
        video: VideoHealth {
            ok: age.is_some_and(|value| value < 2_000),
            size: state.size.to_string(),
            fps: state.fps,
            sequence: state.sequence.load(Ordering::Relaxed),
            age_ms: age,
        },
        input: InputHealth {
            reports: state.hid.reports.load(Ordering::Relaxed),
            last_write_us: state.hid.last_write_us.load(Ordering::Relaxed),
            max_write_us: state.hid.max_write_us.load(Ordering::Relaxed),
        },
    })
}

async fn snapshot(
    State(state): State<AppState>,
    Query(query): Query<SnapshotQuery>,
) -> impl IntoResponse {
    let mut rx = state.frame_rx.clone();
    let after = query.after.unwrap_or(0);
    if rx
        .borrow()
        .as_ref()
        .map_or(true, |frame| frame.sequence <= after)
    {
        let _ = timeout(Duration::from_secs(3), rx.changed()).await;
    }
    let frame = rx.borrow().clone();
    match frame {
        Some(frame) => {
            let mut response = Response::new(Body::from((*frame.jpeg).clone()));
            response
                .headers_mut()
                .insert(header::CONTENT_TYPE, HeaderValue::from_static("image/jpeg"));
            response.headers_mut().insert(
                header::CACHE_CONTROL,
                HeaderValue::from_static("no-store, no-cache, must-revalidate"),
            );
            response.headers_mut().insert(
                "x-dmxt-sequence",
                HeaderValue::from_str(&frame.sequence.to_string()).unwrap(),
            );
            response.headers_mut().insert(
                "x-dmxt-captured-ms",
                HeaderValue::from_str(&frame.captured_ms.to_string()).unwrap(),
            );
            response
        }
        None => Response::builder()
            .status(StatusCode::SERVICE_UNAVAILABLE)
            .body(Body::from("video unavailable"))
            .unwrap(),
    }
}

async fn video_upgrade(ws: WebSocketUpgrade, State(state): State<AppState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| video_socket(socket, state.frame_rx))
}

async fn video_socket(mut socket: WebSocket, mut frames: watch::Receiver<Option<Frame>>) {
    let mut last_sequence = 0;
    loop {
        let frame = frames.borrow_and_update().clone();
        if let Some(frame) = frame {
            if frame.sequence != last_sequence {
                last_sequence = frame.sequence;
                if socket
                    .send(Message::Binary((*frame.jpeg).clone()))
                    .await
                    .is_err()
                {
                    return;
                }
            }
        }
        if frames.changed().await.is_err() {
            return;
        }
    }
}

async fn input_upgrade(ws: WebSocketUpgrade, State(state): State<AppState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| input_socket(socket, state.hid))
}

async fn input_socket(mut socket: WebSocket, hid: Arc<Hid>) {
    while let Some(Ok(message)) = socket.next().await {
        let Message::Binary(report) = message else {
            continue;
        };
        if let Err(error) = hid.handle_input(&report).await {
            eprintln!("input report failed: {error}");
        }
    }
    let _ = hid.release_all();
}

impl Hid {
    fn open() -> io::Result<Self> {
        let open = |path| OpenOptions::new().write(true).open(path);
        Ok(Self {
            keyboard: Mutex::new(open(KEYBOARD)?),
            mouse: Mutex::new(open(MOUSE)?),
            pointer: Mutex::new(open(POINTER)?),
            reports: AtomicU64::new(0),
            last_write_us: AtomicU64::new(0),
            max_write_us: AtomicU64::new(0),
        })
    }

    fn write_report(&self, endpoint: &Mutex<File>, report: &[u8]) -> io::Result<()> {
        let started = Instant::now();
        endpoint
            .lock()
            .map_err(|_| io::Error::new(io::ErrorKind::Other, "HID endpoint lock poisoned"))?
            .write_all(report)?;
        let elapsed = started.elapsed().as_micros() as u64;
        self.reports.fetch_add(1, Ordering::Relaxed);
        self.last_write_us.store(elapsed, Ordering::Relaxed);
        self.max_write_us.fetch_max(elapsed, Ordering::Relaxed);
        Ok(())
    }

    fn release_all(&self) -> io::Result<()> {
        self.write_report(&self.keyboard, &[0; 8])?;
        self.write_report(&self.mouse, &[0; 4])?;
        self.write_report(&self.pointer, &[0; 5])
    }

    async fn handle_input(&self, report: &[u8]) -> io::Result<()> {
        match report {
            [1, dx, dy, wheel] => self.write_report(&self.mouse, &[0, *dx, *dy, *wheel]),
            [2, button, x_low, x_high, y_low, y_high] if matches!(button, 0 | 1 | 2 | 4) => {
                self.write_report(&self.pointer, &[*button, *x_low, *x_high, *y_low, *y_high])
            }
            [3, keyboard @ ..] if keyboard.len() == 8 => {
                self.write_report(&self.keyboard, keyboard)
            }
            [4] => self.release_all(),
            [5, button] if matches!(button, 1 | 2 | 4) => {
                self.write_report(&self.mouse, &[*button, 0, 0, 0])?;
                sleep(Duration::from_millis(16)).await;
                self.write_report(&self.mouse, &[0; 4])
            }
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid input report",
            )),
        }
    }
}
