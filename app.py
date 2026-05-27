import hmac
import hashlib
import json
import uuid
import subprocess
import zipfile
from pathlib import Path

import streamlit as st
from PIL import Image, ImageEnhance

st.set_page_config(page_title="쓰레드 세탁기", page_icon="🧺", layout="centered")

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
    "brighten_video": False,
    "mirror": False,
    "mute": True,
    "image_crop_top": 10,
    "image_crop_bottom": 5,
    "image_brighten": True,
    "image_contrast": True,
    "image_mirror": False,
}

SETTING_KEYS = list(DEFAULT_SETTINGS.keys())
BOOL_SETTING_KEYS = {"brighten_video", "mirror", "mute", "image_brighten", "image_contrast", "image_mirror"}
INT_SETTING_KEYS = {"video_crop_top", "video_crop_bottom", "image_crop_top", "image_crop_bottom"}
FLOAT_SETTING_KEYS = {"trim_head", "trim_tail"}

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _pw_hash(pwd: str) -> str:
    return hashlib.sha256(pwd.encode("utf-8")).hexdigest()


def _setting_query_key(key: str) -> str:
    return f"s_{key}"


def _query_value_to_python(key: str, value):
    if isinstance(value, list):
        value = value[0] if value else ""
    value = str(value)

    if key in BOOL_SETTING_KEYS:
        return value.lower() in {"1", "true", "yes", "on"}
    if key in INT_SETTING_KEYS:
        return int(float(value))
    if key in FLOAT_SETTING_KEYS:
        return float(value)
    return value


def _python_value_to_query(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def widget_key(key: str) -> str:
    return f"w_{key}"


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


def normalize_settings(settings: dict) -> dict:
    merged = DEFAULT_SETTINGS.copy()
    for key in SETTING_KEYS:
        if key in settings:
            val = settings[key]
            try:
                if key in BOOL_SETTING_KEYS:
                    val = bool(val)
                elif key in INT_SETTING_KEYS:
                    val = int(val)
                elif key in FLOAT_SETTING_KEYS:
                    val = float(val)
            except Exception:
                val = DEFAULT_SETTINGS[key]
            merged[key] = val
    return merged


def _write_file(settings: dict) -> None:
    try:
        SETTINGS_PATH.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _read_file() -> dict:
    try:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _read_url_settings() -> dict:
    result = {}
    try:
        for key in SETTING_KEYS:
            qkey = _setting_query_key(key)
            if qkey in st.query_params:
                result[key] = _query_value_to_python(key, st.query_params.get(qkey))
    except Exception:
        pass
    return result


def _write_url_settings(settings: dict) -> None:
    try:
        for key, value in settings.items():
            if key in SETTING_KEYS:
                st.query_params[_setting_query_key(key)] = _python_value_to_query(value)
    except Exception:
        pass


def init_and_sync_settings() -> dict:
    """
    매 re-run 시작 시 호출.
    1) 이전 run의 위젯 값이 session_state에 남아있으면 캡처
    2) 캡처한 값 + 기존 저장 값 병합 → 파일 저장
    3) 위젯 키 초기화 (위젯 생성 전에 session_state에 넣어야 함)
    """
    # --- 1단계: 이전 run 위젯 값 캡처 (cleanup 전이라 아직 존재) ---
    captured = {}
    for key in SETTING_KEYS:
        wk = widget_key(key)
        if wk in st.session_state:
            captured[key] = st.session_state[wk]

    # --- 2단계: 저장된 설정 로드 (최초 1회) ---
    if "_persisted_settings" not in st.session_state:
        url_s = _read_url_settings()
        file_s = _read_file()
        merged = DEFAULT_SETTINGS.copy()
        merged.update(file_s)
        merged.update(url_s)
        st.session_state["_persisted_settings"] = normalize_settings(merged)

    # --- 3단계: 캡처된 위젯 값으로 업데이트 ---
    current = dict(st.session_state["_persisted_settings"])
    if captured:
        current.update(captured)
        current = normalize_settings(current)
        if current != st.session_state["_persisted_settings"]:
            st.session_state["_persisted_settings"] = current
            _write_file(current)
            st.session_state["_settings_changed"] = True

    # --- 4단계: 위젯 키 초기화 (없는 것만) ---
    for key, value in current.items():
        wk = widget_key(key)
        if wk not in st.session_state:
            st.session_state[wk] = value

    return current


def current_settings() -> dict:
    return normalize_settings(st.session_state.get("_persisted_settings", DEFAULT_SETTINGS.copy()))


def check_password() -> bool:
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False

    token = _pw_hash(APP_PASSWORD)

    if not st.session_state["password_correct"]:
        try:
            auth_token = st.query_params.get("auth")
            if auth_token and hmac.compare_digest(str(auth_token), token):
                st.session_state["password_correct"] = True
        except Exception:
            pass

    if st.session_state["password_correct"]:
        return True

    st.title("🔒 쓰레드 세탁기")
    st.caption("비밀번호를 입력하세요.")

    pwd = st.text_input("비밀번호", type="password")
    remember = st.checkbox("로그인 상태 유지")
    if remember:
        st.caption("로그인 후 현재 주소를 홈화면/북마크에 저장하세요. 이 주소는 다른 사람에게 공유하지 마세요.")

    if st.button("접속", use_container_width=True):
        if hmac.compare_digest(str(pwd), APP_PASSWORD):
            st.session_state["password_correct"] = True
            if remember:
                try:
                    st.query_params["auth"] = token
                except Exception:
                    pass
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

    if settings["brighten_video"]:
        filters.append("eq=brightness=0.03:contrast=1.02")
    if settings["mirror"]:
        filters.append("hflip")

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

    cmd += [
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-movflags", "+faststart",
        str(output_path),
    ]

    run_cmd(cmd)


def process_image(uploaded_file, output_path: Path, settings: dict) -> None:
    try:
        uploaded_file.seek(0)
    except Exception:
        pass

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
    if settings["image_mirror"]:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

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

init_and_sync_settings()

st.title("🧺 쓰레드 세탁기")
st.caption("영상과 이미지를 업로드해서 간단히 정리합니다.")

st.caption(
    f"앱 내부 제한: 영상 {MAX_VIDEO_MB}MB 이하 / 이미지 {MAX_IMAGE_MB}MB 이하 / 한 번에 최대 {MAX_TOTAL_FILES}개"
)

uploaded_files = st.file_uploader(
    "영상/이미지 파일 업로드",
    type=["mp4", "mov", "mkv", "webm", "jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
)

if uploaded_files:
    with st.container(border=True):
        st.success(f"✅ 업로드 완료: {len(uploaded_files)}개 파일")

        image_preview_count = 0
        for f in uploaded_files:
            kind = classify_file(f.name)
            size_mb = f.size / (1024 * 1024)
            icon = "📹" if kind == "video" else "🖼️" if kind == "image" else "⚠️"
            st.write(f"{icon} {f.name} — {size_mb:.1f}MB")

            if kind == "image" and image_preview_count < 2:
                try:
                    f.seek(0)
                    st.image(f, caption=f.name, use_container_width=True)
                    f.seek(0)
                    image_preview_count += 1
                except Exception:
                    pass

        st.info("아래에서 옵션을 확인한 뒤 전체 처리 시작을 누르세요.")
else:
    st.caption("파일을 업로드하면 이 위치에 업로드 완료 표시가 나옵니다.")

st.markdown("---")

option_mode = st.selectbox(
    "편집 옵션 열기",
    ["옵션 숨김", "영상 옵션", "이미지 옵션"],
    index=0,
    key="option_mode",
)

if option_mode == "영상 옵션":
    with st.container(border=True):
        st.subheader("📹 영상 옵션")
        st.number_input("앞부분 자르기(초)", 0.0, 10.0, step=0.1, key=widget_key("trim_head"))
        st.number_input("뒷부분 자르기(초)", 0.0, 10.0, step=0.1, key=widget_key("trim_tail"))
        st.number_input("영상 상단 크롭(%)", min_value=0, max_value=40, step=1, key=widget_key("video_crop_top"))
        st.number_input("영상 하단 크롭(%)", min_value=0, max_value=40, step=1, key=widget_key("video_crop_bottom"))
        st.checkbox("영상 밝기/대비 약간 보정", key=widget_key("brighten_video"))
        st.checkbox("좌우 반전", key=widget_key("mirror"))
        st.checkbox("무음 처리", key=widget_key("mute"))

        s = current_settings()
        if s["video_crop_top"] + s["video_crop_bottom"] >= 90:
            st.warning("영상 크롭 합계가 너무 큽니다. 합계 90% 미만 권장.")

elif option_mode == "이미지 옵션":
    with st.container(border=True):
        st.subheader("🖼️ 이미지 옵션")
        st.number_input("이미지 상단 크롭(%)", min_value=0, max_value=40, step=1, key=widget_key("image_crop_top"))
        st.number_input("이미지 하단 크롭(%)", min_value=0, max_value=40, step=1, key=widget_key("image_crop_bottom"))
        st.checkbox("이미지 밝기 약간 보정", key=widget_key("image_brighten"))
        st.checkbox("이미지 보정", key=widget_key("image_contrast"))
        st.checkbox("이미지 좌우 반전", key=widget_key("image_mirror"))

        s = current_settings()
        if s["image_crop_top"] + s["image_crop_bottom"] >= 90:
            st.warning("이미지 크롭 합계가 너무 큽니다. 합계 90% 미만 권장.")

if st.session_state.pop("_settings_changed", False):
    st.success("설정 저장됨")
    _write_url_settings(current_settings())

settings = current_settings()

st.markdown("---")

if uploaded_files:
    st.subheader("처리 준비")

    if len(uploaded_files) > MAX_TOTAL_FILES:
        st.error(f"파일은 한 번에 최대 {MAX_TOTAL_FILES}개까지만 처리합니다.")
    else:
        video_count = 0
        image_count = 0
        blocked = False

        for f in uploaded_files:
            kind = classify_file(f.name)
            size_mb = f.size / (1024 * 1024)

            if kind == "video":
                video_count += 1
                if size_mb > MAX_VIDEO_MB:
                    blocked = True
                    st.error(f"📹 {f.name}: 용량 초과 {size_mb:.1f}MB / 최대 {MAX_VIDEO_MB}MB")
            elif kind == "image":
                image_count += 1
                if size_mb > MAX_IMAGE_MB:
                    blocked = True
                    st.error(f"🖼️ {f.name}: 용량 초과 {size_mb:.1f}MB / 최대 {MAX_IMAGE_MB}MB")
            else:
                blocked = True
                st.error(f"{f.name}: 지원하지 않는 파일 형식")

        st.info(f"영상 {video_count}개 / 이미지 {image_count}개")

        crop_invalid = (
            not validate_crop(settings["video_crop_top"], settings["video_crop_bottom"])
            or not validate_crop(settings["image_crop_top"], settings["image_crop_bottom"])
        )

        if crop_invalid:
            blocked = True
            st.error("크롭 합계가 너무 큽니다. 영상/이미지 크롭 합계를 각각 90% 미만으로 설정하세요.")

        st.warning("처리 중에는 화면을 끄거나 다른 앱으로 이동하지 마세요. 완료 후 바로 다운로드하세요.")

        if st.button("전체 처리 시작", type="primary", disabled=blocked, use_container_width=True):
            result_paths: list[Path] = []
            processed_count = 0
            failed_count = 0

            st.markdown("### 진행 상황")
            progress = st.progress(0, text="처리 대기 중...")
            status = st.empty()
            count_box = st.empty()

            try:
                total = len(uploaded_files)

                for idx, f in enumerate(uploaded_files, start=1):
                    kind = classify_file(f.name)
                    percent = int((idx - 1) / total * 100)

                    status.info(f"처리 중: {idx}/{total} — {f.name}")
                    count_box.write(f"진행률: {percent}%")

                    try:
                        if kind == "video":
                            process_one_video(f, settings, result_paths)
                            processed_count += 1
                        elif kind == "image":
                            try:
                                f.seek(0)
                            except Exception:
                                pass
                            process_one_image(f, settings, result_paths)
                            processed_count += 1
                        else:
                            failed_count += 1
                    except Exception as e:
                        failed_count += 1
                        st.error(f"{f.name} 처리 실패: {e}")

                    percent = int(idx / total * 100)
                    progress.progress(idx / total, text=f"{percent}% 완료")
                    count_box.write(f"진행률: {percent}%")

                status.success("처리 완료")
                progress.progress(1.0, text="100% 완료")

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

                        st.download_button(
                            "결과 파일 다운로드",
                            data=data,
                            file_name=p.name,
                            mime=mime,
                            use_container_width=True,
                        )
                    else:
                        zip_bytes = make_zip(result_paths)
                        st.download_button(
                            "결과 ZIP 다운로드",
                            data=zip_bytes,
                            file_name="edited_results.zip",
                            mime="application/zip",
                            use_container_width=True,
                        )
                else:
                    st.error("처리된 결과가 없습니다.")
            finally:
                safe_delete(*result_paths)
else:
    st.info("파일을 업로드하면 처리 버튼이 표시됩니다.")
