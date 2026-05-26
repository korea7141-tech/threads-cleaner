import hmac
import json
import uuid
import subprocess
import zipfile
from pathlib import Path

import streamlit as st
from PIL import Image, ImageEnhance

st.set_page_config(page_title="Upload Media Editor", page_icon="🎬", layout="centered")

APP_PASSWORD = str(st.secrets.get("APP_PASSWORD", "1234"))
MAX_VIDEO_MB = int(st.secrets.get("MAX_VIDEO_MB", 50))
MAX_IMAGE_MB = int(st.secrets.get("MAX_IMAGE_MB", 10))
MAX_TOTAL_FILES = int(st.secrets.get("MAX_TOTAL_FILES", 10))

WORKDIR = Path("work")
WORKDIR.mkdir(exist_ok=True)

SETTINGS_PATH = WORKDIR / "editor_settings.json"

DEFAULT_SETTINGS = {
    "trim_head": 0.5,
    "trim_tail": 0.5,
    "video_crop_top": 15,
    "video_crop_bottom": 10,
    "mute": True,
    "mirror": False,
    "brighten_video": False,
    "image_crop_top": 10,
    "image_crop_bottom": 5,
    "image_brighten": True,
    "image_contrast": True,
    "auto_save_settings": False,
}

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def cleanup_old_work_files() -> None:
    for pattern in ["in_video_*", "out_video_*", "out_image_*", "mixed_result_*", "*.tmp"]:
        for p in WORKDIR.glob(pattern):
            try:
                if p.is_file():
                    p.unlink()
            except Exception:
                pass


if "workdir_cleaned" not in st.session_state:
    cleanup_old_work_files()
    st.session_state["workdir_cleaned"] = True


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
        "ffprobe", "-v", "error",
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


def load_saved_settings() -> dict:
    try:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = DEFAULT_SETTINGS.copy()
                for key in DEFAULT_SETTINGS:
                    if key in data:
                        merged[key] = data[key]
                return merged
    except Exception:
        pass
    return DEFAULT_SETTINGS.copy()


def apply_settings_to_session(settings: dict) -> None:
    for key, value in settings.items():
        if key not in st.session_state:
            st.session_state[key] = value


def current_settings_from_session() -> dict:
    return {
        "trim_head": float(st.session_state.get("trim_head", DEFAULT_SETTINGS["trim_head"])),
        "trim_tail": float(st.session_state.get("trim_tail", DEFAULT_SETTINGS["trim_tail"])),
        "video_crop_top": int(st.session_state.get("video_crop_top", DEFAULT_SETTINGS["video_crop_top"])),
        "video_crop_bottom": int(st.session_state.get("video_crop_bottom", DEFAULT_SETTINGS["video_crop_bottom"])),
        "mute": bool(st.session_state.get("mute", DEFAULT_SETTINGS["mute"])),
        "mirror": bool(st.session_state.get("mirror", DEFAULT_SETTINGS["mirror"])),
        "brighten_video": bool(st.session_state.get("brighten_video", DEFAULT_SETTINGS["brighten_video"])),
        "image_crop_top": int(st.session_state.get("image_crop_top", DEFAULT_SETTINGS["image_crop_top"])),
        "image_crop_bottom": int(st.session_state.get("image_crop_bottom", DEFAULT_SETTINGS["image_crop_bottom"])),
        "image_brighten": bool(st.session_state.get("image_brighten", DEFAULT_SETTINGS["image_brighten"])),
        "image_contrast": bool(st.session_state.get("image_contrast", DEFAULT_SETTINGS["image_contrast"])),
        "auto_save_settings": bool(st.session_state.get("auto_save_settings", DEFAULT_SETTINGS.get("auto_save_settings", False))),
    }


def save_current_settings() -> None:
    SETTINGS_PATH.write_text(
        json.dumps(current_settings_from_session(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def auto_save_if_enabled() -> None:
    if not st.session_state.get("auto_save_settings", False):
        return

    current = current_settings_from_session()
    last = st.session_state.get("_last_auto_saved_settings")
    if last == current:
        return

    SETTINGS_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    st.session_state["_last_auto_saved_settings"] = current.copy()


def reset_settings_to_default() -> None:
    for key, value in DEFAULT_SETTINGS.items():
        st.session_state[key] = value
    safe_delete(SETTINGS_PATH)


def check_password() -> bool:
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False
    if st.session_state["password_correct"]:
        return True

    st.title("🔒 쓰레드 세타기")
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


def get_ext(filename: str) -> str:
    return Path(filename).suffix.lower()


def classify_file(filename: str) -> str:
    ext = get_ext(filename)
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return "unknown"


def safe_stem(filename: str) -> str:
    stem = Path(filename).stem.strip()
    if not stem:
        return "file"

    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in stem)
    safe = safe.strip("_")

    return safe[:60] if safe else "file"


def process_video_ffmpeg(input_path: Path, output_path: Path, settings: dict) -> None:
    duration = get_video_duration(input_path)
    trim_head = settings["trim_head"]
    trim_tail = settings["trim_tail"]

    can_trim = duration > (trim_head + trim_tail + 0.5)
    start = trim_head if can_trim else 0.0
    out_duration = duration - start - (trim_tail if can_trim else 0.0)
    if out_duration <= 0:
        raise RuntimeError("편집 후 영상 길이가 0초 이하입니다. 자르기 값을 줄이세요.")

    crop_top = settings["video_crop_top"]
    crop_bottom = settings["video_crop_bottom"]
    remain_ratio = max(0.1, 1 - (crop_top / 100) - (crop_bottom / 100))
    crop_y = crop_top / 100

    filters = [f"crop=w=iw:h=ih*{remain_ratio:.6f}:x=0:y=ih*{crop_y:.6f}"]
    if settings["mirror"]:
        filters.append("hflip")
    if settings["brighten_video"]:
        filters.append("eq=brightness=0.03:contrast=1.02")

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", str(input_path),
        "-t", str(out_duration),
        "-vf", ",".join(filters),
    ]

    if settings["mute"]:
        cmd += ["-an"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]

    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-movflags", "+faststart", str(output_path)]
    run_cmd(cmd)


def process_image(uploaded_file, output_path: Path, settings: dict) -> None:
    img = Image.open(uploaded_file)
    if img.mode != "RGB":
        img = img.convert("RGB")

    crop_top = settings["image_crop_top"]
    crop_bottom = settings["image_crop_bottom"]
    if crop_top or crop_bottom:
        w, h = img.size
        y1 = int(h * crop_top / 100)
        y2 = int(h * (1 - crop_bottom / 100))
        if y2 <= y1:
            raise RuntimeError("이미지 크롭 값이 너무 큽니다.")
        img = img.crop((0, y1, w, y2))

    if settings["image_brighten"]:
        img = ImageEnhance.Brightness(img).enhance(1.02)
    if settings["image_contrast"]:
        img = ImageEnhance.Contrast(img).enhance(1.02)

    img.save(output_path, format="JPEG", quality=95, optimize=True)


def process_one_video(uploaded_file, settings: dict, result_paths: list[Path]) -> Path:
    job_id = uuid.uuid4().hex[:8]
    suffix = get_ext(uploaded_file.name) or ".mp4"
    input_path = WORKDIR / f"in_video_{job_id}{suffix}"
    temp_output = WORKDIR / f"out_video_{job_id}.mp4"
    final_path = WORKDIR / f"mixed_result_{job_id}_{safe_stem(uploaded_file.name)}.mp4"

    try:
        with open(input_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        process_video_ffmpeg(input_path, temp_output, settings)
        temp_output.rename(final_path)
        result_paths.append(final_path)
        return final_path
    finally:
        safe_delete(input_path, temp_output)


def process_one_image(uploaded_file, settings: dict, result_paths: list[Path]) -> Path:
    job_id = uuid.uuid4().hex[:8]
    final_path = WORKDIR / f"mixed_result_{job_id}_{safe_stem(uploaded_file.name)}.jpg"

    try:
        process_image(uploaded_file, final_path, settings)
        result_paths.append(final_path)
        return final_path
    except Exception:
        safe_delete(final_path)
        raise


def make_zip(paths: list[Path]) -> bytes:
    zip_path = WORKDIR / f"mixed_result_{uuid.uuid4().hex[:8]}.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in paths:
                if p.exists():
                    z.write(p, arcname=p.name)
        return zip_path.read_bytes()
    finally:
        safe_delete(zip_path)


if not check_password():
    st.stop()

if "settings_loaded" not in st.session_state:
    apply_settings_to_session(load_saved_settings())
    st.session_state["settings_loaded"] = True

st.title("🎬 쓰레드 세타기")
st.caption("영상과 이미지를 따로 또는 같이 업로드해서 처리합니다.")

with st.expander("⚙️ 설정 저장", expanded=False):
    st.caption("크롭값과 체크박스 옵션을 저장합니다. 저장된 값은 다음 접속 시부터 기본값으로 적용됩니다. 공유 앱에서는 모든 사용자가 같은 저장값을 사용합니다.")

    auto_save_settings = st.checkbox("설정 자동 저장", key="auto_save_settings")

    col_save, col_reset = st.columns(2)
    with col_save:
        if st.button("현재 설정 저장", use_container_width=True):
            save_current_settings()
            st.session_state["settings_saved_message"] = True
            st.rerun()
    with col_reset:
        if st.button("기본값 복원", use_container_width=True):
            reset_settings_to_default()
            st.session_state["settings_reset_message"] = True
            st.rerun()

    if auto_save_settings:
        st.info("자동 저장 켜짐: 옵션을 바꾸면 자동으로 저장됩니다.")

if st.session_state.pop("settings_saved_message", False):
    st.success("설정 저장 완료")

if st.session_state.pop("settings_reset_message", False):
    st.success("기본값으로 복원했습니다.")

st.caption(
    f"앱 내부 제한: 영상 {MAX_VIDEO_MB}MB 이하 / 이미지 {MAX_IMAGE_MB}MB 이하 / 한 번에 최대 {MAX_TOTAL_FILES}개"
)

uploaded_files = st.file_uploader(
    "영상/이미지 파일 업로드",
    type=["mp4", "mov", "mkv", "webm", "jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
)

with st.expander("📹 영상 옵션", expanded=False):
    col1, col2 = st.columns(2)
    with col1:
        trim_head = st.number_input("앞부분 자르기(초)", 0.0, 10.0, step=0.1, key="trim_head")
        video_crop_top = st.number_input("영상 상단 크롭(%)", min_value=0, max_value=40, step=1, key="video_crop_top")
        mute = st.checkbox("무음 처리", key="mute")
    with col2:
        trim_tail = st.number_input("뒷부분 자르기(초)", 0.0, 10.0, step=0.1, key="trim_tail")
        video_crop_bottom = st.number_input("영상 하단 크롭(%)", min_value=0, max_value=40, step=1, key="video_crop_bottom")
        mirror = st.checkbox("좌우 반전", key="mirror")
    brighten_video = st.checkbox("영상 밝기/대비 약간 보정", key="brighten_video")
    if video_crop_top + video_crop_bottom >= 90:
        st.warning("영상 크롭 합계가 너무 큽니다. 합계 90% 미만 권장.")

with st.expander("🖼️ 이미지 옵션", expanded=False):
    col1, col2 = st.columns(2)
    with col1:
        image_crop_top = st.number_input("이미지 상단 크롭(%)", min_value=0, max_value=40, step=1, key="image_crop_top")
        image_brighten = st.checkbox("이미지 밝기 약간 보정", key="image_brighten")
    with col2:
        image_crop_bottom = st.number_input("이미지 하단 크롭(%)", min_value=0, max_value=40, step=1, key="image_crop_bottom")
        image_contrast = st.checkbox("이미지 대비 약간 보정", key="image_contrast")
    if image_crop_top + image_crop_bottom >= 90:
        st.warning("이미지 크롭 합계가 너무 큽니다. 합계 90% 미만 권장.")

auto_save_if_enabled()

settings = {
    "trim_head": trim_head,
    "trim_tail": trim_tail,
    "video_crop_top": video_crop_top,
    "video_crop_bottom": video_crop_bottom,
    "mute": mute,
    "mirror": mirror,
    "brighten_video": brighten_video,
    "image_crop_top": image_crop_top,
    "image_crop_bottom": image_crop_bottom,
    "image_brighten": image_brighten,
    "image_contrast": image_contrast,
}

if uploaded_files:
    st.markdown("---")
    st.subheader("업로드 목록")

    if len(uploaded_files) > MAX_TOTAL_FILES:
        st.error(f"파일은 한 번에 최대 {MAX_TOTAL_FILES}개까지만 처리합니다.")
    else:
        video_count = image_count = unknown_count = 0
        blocked = False

        for f in uploaded_files:
            kind = classify_file(f.name)
            size_mb = f.size / (1024 * 1024)
            if kind == "video":
                video_count += 1
                if size_mb > MAX_VIDEO_MB:
                    blocked = True
                    st.error(f"📹 {f.name}: 용량 초과 {size_mb:.1f}MB / 최대 {MAX_VIDEO_MB}MB")
                else:
                    st.write(f"📹 {f.name} — {size_mb:.1f}MB")
            elif kind == "image":
                image_count += 1
                if size_mb > MAX_IMAGE_MB:
                    blocked = True
                    st.error(f"🖼️ {f.name}: 용량 초과 {size_mb:.1f}MB / 최대 {MAX_IMAGE_MB}MB")
                else:
                    st.write(f"🖼️ {f.name} — {size_mb:.1f}MB")
            else:
                unknown_count += 1
                blocked = True
                st.error(f"{f.name}: 지원하지 않는 파일 형식")

        st.info(f"영상 {video_count}개 / 이미지 {image_count}개")

        crop_invalid = (
            not validate_crop(video_crop_top, video_crop_bottom)
            or not validate_crop(image_crop_top, image_crop_bottom)
        )
        if crop_invalid:
            blocked = True
            st.error("크롭 합계가 너무 큽니다. 영상/이미지 크롭 합계를 각각 90% 미만으로 설정하세요.")

        if st.button("전체 처리 시작", type="primary", disabled=blocked):
            result_paths: list[Path] = []
            processed_count = 0
            failed_count = 0
            progress = st.progress(0)
            status = st.empty()

            try:
                total = len(uploaded_files)
                for idx, f in enumerate(uploaded_files, start=1):
                    kind = classify_file(f.name)
                    status.write(f"처리 중: {f.name}")
                    try:
                        if kind == "video":
                            process_one_video(f, settings, result_paths)
                            processed_count += 1
                        elif kind == "image":
                            process_one_image(f, settings, result_paths)
                            processed_count += 1
                        else:
                            failed_count += 1
                    except Exception as e:
                        failed_count += 1
                        st.error(f"{f.name} 처리 실패: {e}")
                    progress.progress(idx / total)

                if result_paths:
                    st.success(f"처리 완료: 성공 {processed_count}개 / 실패 {failed_count}개")
                    if len(result_paths) == 1:
                        p = result_paths[0]
                        data = p.read_bytes()
                        if p.suffix.lower() == ".mp4":
                            st.video(data)
                            mime = "video/mp4"
                        else:
                            st.image(data)
                            mime = "image/jpeg"
                        st.download_button("결과 파일 다운로드", data=data, file_name=p.name, mime=mime, use_container_width=True)
                    else:
                        zip_bytes = make_zip(result_paths)
                        st.download_button("결과 ZIP 다운로드", data=zip_bytes, file_name="edited_results.zip", mime="application/zip", use_container_width=True)
                else:
                    st.error("처리된 결과가 없습니다.")
            finally:
                safe_delete(*result_paths)
