import hmac
import uuid
import subprocess
from pathlib import Path

import streamlit as st
from PIL import Image, ImageEnhance

# Streamlit 설정은 반드시 첫 Streamlit 명령이어야 함
st.set_page_config(page_title="Upload Media Editor", page_icon="🎬", layout="centered")

# =========================
# 기본 설정
# =========================
APP_PASSWORD = str(st.secrets.get("APP_PASSWORD", "1234"))
MAX_VIDEO_MB = int(st.secrets.get("MAX_VIDEO_MB", 50))
MAX_IMAGE_MB = int(st.secrets.get("MAX_IMAGE_MB", 10))

WORKDIR = Path("work")
WORKDIR.mkdir(exist_ok=True)


# =========================
# 앱 시작 시 이전 임시파일 정리
# =========================
def cleanup_old_work_files() -> None:
    patterns = [
        "in_video_*",
        "out_video_*",
        "out_image_*",
        "*.tmp",
    ]
    for pattern in patterns:
        for p in WORKDIR.glob(pattern):
            try:
                if p.is_file():
                    p.unlink()
            except Exception:
                pass


if "workdir_cleaned" not in st.session_state:
    cleanup_old_work_files()
    st.session_state["workdir_cleaned"] = True


# =========================
# 공통 유틸
# =========================
def run_cmd(cmd: list[str]) -> str:
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "command failed").strip())
    return (p.stdout or "").strip()


def get_video_duration(input_path: Path) -> float:
    out = run_cmd([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        str(input_path),
    ])
    try:
        duration = float(out)
    except Exception as exc:
        raise RuntimeError("영상 길이를 읽지 못했습니다. 파일 포맷을 확인하세요.") from exc

    if duration <= 0:
        raise RuntimeError("영상 길이를 읽지 못했습니다. 파일 포맷을 확인하세요.")

    return duration


def safe_delete(*paths: Path) -> None:
    for p in paths:
        try:
            if p and Path(p).exists():
                Path(p).unlink()
        except Exception:
            pass


def check_password() -> bool:
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    if st.session_state["password_correct"]:
        return True

    st.title("🔒 Upload Media Editor")
    st.caption("비밀번호를 입력하세요.")

    pwd = st.text_input("비밀번호", type="password")
    if st.button("접속"):
        if hmac.compare_digest(str(pwd), APP_PASSWORD):
            st.session_state["password_correct"] = True
            st.rerun()
        else:
            st.error("비밀번호가 일치하지 않습니다.")

    return False


def validate_crop(crop_top: int, crop_bottom: int) -> bool:
    return crop_top + crop_bottom < 90


# =========================
# 영상 처리
# =========================
def process_video_ffmpeg(
    input_path: Path,
    output_path: Path,
    trim_head: float,
    trim_tail: float,
    crop_top: int,
    crop_bottom: int,
    mute: bool,
    mirror: bool,
    brighten: bool,
) -> None:
    duration = get_video_duration(input_path)

    can_trim = duration > (trim_head + trim_tail + 0.5)
    start = trim_head if can_trim else 0.0
    out_duration = duration - start - (trim_tail if can_trim else 0.0)

    if out_duration <= 0:
        raise RuntimeError("편집 후 영상 길이가 0초 이하입니다. 자르기 값을 줄이세요.")

    remain_ratio = max(0.1, 1 - (crop_top / 100) - (crop_bottom / 100))
    crop_y = crop_top / 100

    filters = [
        f"crop=w=iw:h=ih*{remain_ratio:.6f}:x=0:y=ih*{crop_y:.6f}"
    ]

    if mirror:
        filters.append("hflip")

    if brighten:
        filters.append("eq=brightness=0.03:contrast=1.02")

    vf = ",".join(filters)

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(start),
        "-i", str(input_path),
        "-t", str(out_duration),
        "-vf", vf,
    ]

    if mute:
        cmd += ["-an"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]

    cmd += [
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-movflags", "+faststart",
        str(output_path),
    ]

    run_cmd(cmd)


# =========================
# 이미지 처리
# =========================
def process_image(
    uploaded_file,
    output_path: Path,
    crop_top: int,
    crop_bottom: int,
    brighten: bool,
    contrast: bool,
) -> None:
    img = Image.open(uploaded_file)

    if img.mode != "RGB":
        img = img.convert("RGB")

    if crop_top or crop_bottom:
        w, h = img.size
        y1 = int(h * crop_top / 100)
        y2 = int(h * (1 - crop_bottom / 100))
        if y2 <= y1:
            raise RuntimeError("이미지 크롭 값이 너무 큽니다.")
        img = img.crop((0, y1, w, y2))

    if brighten:
        img = ImageEnhance.Brightness(img).enhance(1.02)

    if contrast:
        img = ImageEnhance.Contrast(img).enhance(1.02)

    img.save(output_path, format="JPEG", quality=95, optimize=True)


# =========================
# 접근 제어
# =========================
if not check_password():
    st.stop()


# =========================
# UI
# =========================
st.title("🎬 Upload Media Editor")
st.caption("업로드한 영상/이미지를 간단히 편집하고 다운로드합니다.")

tab_video, tab_image = st.tabs(["📹 영상", "🖼️ 이미지"])


with tab_video:
    st.subheader("영상 편집")

    v_file = st.file_uploader(
        f"영상 파일 업로드, 최대 {MAX_VIDEO_MB}MB",
        type=["mp4", "mov", "mkv", "webm"],
        key="video_file",
    )

    col1, col2 = st.columns(2)
    with col1:
        trim_head = st.number_input("앞부분 자르기(초)", 0.0, 10.0, 0.5, 0.1)
        crop_top = st.slider("상단 크롭(%)", 0, 40, 15)
        mute = st.checkbox("무음 처리", value=True)
    with col2:
        trim_tail = st.number_input("뒷부분 자르기(초)", 0.0, 10.0, 0.5, 0.1)
        crop_bottom = st.slider("하단 크롭(%)", 0, 40, 10)
        mirror = st.checkbox("좌우 반전", value=False)

    brighten_video = st.checkbox("밝기/대비 약간 보정", value=False, key="brighten_video")

    if crop_top + crop_bottom >= 90:
        st.warning("크롭 합계가 너무 큽니다. 합계 90% 미만 권장.")

    if v_file:
        size_mb = v_file.size / (1024 * 1024)
        st.info(f"파일 용량: {size_mb:.1f}MB")

        if size_mb > MAX_VIDEO_MB:
            st.error(f"용량 초과: 최대 {MAX_VIDEO_MB}MB까지만 허용됩니다.")
        elif st.button("영상 처리 시작", type="primary"):
            if not validate_crop(crop_top, crop_bottom):
                st.error("상단 크롭과 하단 크롭 합계가 너무 큽니다. 합계 90% 미만으로 설정하세요.")
            else:
                job_id = uuid.uuid4().hex[:8]
                suffix = Path(v_file.name).suffix or ".mp4"
                input_path = WORKDIR / f"in_video_{job_id}{suffix}"
                output_path = WORKDIR / f"out_video_{job_id}.mp4"

                try:
                    # 영상은 FFmpeg 처리를 위해 실제 임시 입력 파일이 필요함
                    with open(input_path, "wb") as f:
                        f.write(v_file.getbuffer())

                    with st.spinner("영상 처리 중..."):
                        process_video_ffmpeg(
                            input_path=input_path,
                            output_path=output_path,
                            trim_head=trim_head,
                            trim_tail=trim_tail,
                            crop_top=crop_top,
                            crop_bottom=crop_bottom,
                            mute=mute,
                            mirror=mirror,
                            brighten=brighten_video,
                        )

                    result_bytes = output_path.read_bytes()
                    st.success("완료")
                    st.video(result_bytes)

                    st.download_button(
                        label="영상 다운로드",
                        data=result_bytes,
                        file_name="edited_video.mp4",
                        mime="video/mp4",
                        use_container_width=True,
                    )

                except Exception as e:
                    st.error(f"처리 실패: {e}")

                finally:
                    # input_path: FFmpeg용 임시 입력 파일 / output_path: 다운로드 버튼용 bytes 확보 후 삭제
                    safe_delete(input_path, output_path)


with tab_image:
    st.subheader("이미지 편집")

    i_file = st.file_uploader(
        f"이미지 파일 업로드, 최대 {MAX_IMAGE_MB}MB",
        type=["jpg", "jpeg", "png", "webp"],
        key="image_file",
    )

    col1, col2 = st.columns(2)
    with col1:
        i_crop_top = st.slider("이미지 상단 크롭(%)", 0, 40, 10)
        i_brighten = st.checkbox("밝기 약간 보정", value=True)
    with col2:
        i_crop_bottom = st.slider("이미지 하단 크롭(%)", 0, 40, 5)
        i_contrast = st.checkbox("대비 약간 보정", value=True)

    if i_crop_top + i_crop_bottom >= 90:
        st.warning("크롭 합계가 너무 큽니다. 합계 90% 미만 권장.")

    if i_file:
        size_mb = i_file.size / (1024 * 1024)
        st.info(f"파일 용량: {size_mb:.1f}MB")

        if size_mb > MAX_IMAGE_MB:
            st.error(f"용량 초과: 최대 {MAX_IMAGE_MB}MB까지만 허용됩니다.")
        elif st.button("이미지 처리 시작", type="primary"):
            if not validate_crop(i_crop_top, i_crop_bottom):
                st.error("상단 크롭과 하단 크롭 합계가 너무 큽니다. 합계 90% 미만으로 설정하세요.")
            else:
                job_id = uuid.uuid4().hex[:8]
                output_path = WORKDIR / f"out_image_{job_id}.jpg"

                try:
                    with st.spinner("이미지 처리 중..."):
                        process_image(
                            uploaded_file=i_file,
                            output_path=output_path,
                            crop_top=i_crop_top,
                            crop_bottom=i_crop_bottom,
                            brighten=i_brighten,
                            contrast=i_contrast,
                        )

                    result_bytes = output_path.read_bytes()
                    st.success("완료")
                    st.image(result_bytes)

                    st.download_button(
                        label="이미지 다운로드",
                        data=result_bytes,
                        file_name="edited_image.jpg",
                        mime="image/jpeg",
                        use_container_width=True,
                    )

                except Exception as e:
                    st.error(f"처리 실패: {e}")

                finally:
                    safe_delete(output_path)
