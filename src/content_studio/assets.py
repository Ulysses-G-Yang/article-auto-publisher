"""受控内容图片存储。"""

import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from content_studio.errors import ContentAssetError
from content_studio.runtime_paths import default_asset_root

MAX_ASSET_BYTES = 20 * 1024 * 1024
FORMAT_METADATA = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "GIF": (".gif", "image/gif"),
    "WEBP": (".webp", "image/webp"),
}


@dataclass(frozen=True, slots=True)
class StoredAsset:
    asset_id: str
    original_filename: str
    storage_path: str
    media_type: str
    size_bytes: int
    sha256: str
    width: int
    height: int


class AssetStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or default_asset_root()).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save_image(self, data: bytes, original_filename: str) -> StoredAsset:
        if not data:
            raise ContentAssetError("图片文件为空")
        if len(data) > MAX_ASSET_BYTES:
            raise ContentAssetError("单张图片不能超过 20MB")
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                width, height = image.size
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ContentAssetError("文件不是受支持的有效图片") from exc
        if image_format not in FORMAT_METADATA:
            raise ContentAssetError("仅支持 JPEG、PNG、GIF 和 WebP 图片")

        extension, media_type = FORMAT_METADATA[image_format]
        asset_id = str(uuid.uuid4())
        digest = hashlib.sha256(data).hexdigest()
        target_dir = (self.root / digest[:2]).resolve()
        self._assert_within_root(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = (target_dir / f"{asset_id}{extension}").resolve()
        self._assert_within_root(target)
        target.write_bytes(data)
        safe_name = secure_filename(original_filename or "") or f"image{extension}"
        return StoredAsset(
            asset_id=asset_id,
            original_filename=safe_name[:255],
            storage_path=str(target),
            media_type=media_type,
            size_bytes=len(data),
            sha256=digest,
            width=width,
            height=height,
        )

    def resolve(self, storage_path: str) -> Path:
        target = Path(storage_path).expanduser().resolve(strict=True)
        self._assert_within_root(target)
        if not target.is_file():
            raise ContentAssetError("图片文件不存在")
        return target

    def remove_if_owned(self, storage_path: str) -> None:
        target = Path(storage_path).expanduser().resolve()
        self._assert_within_root(target)
        if target.is_file():
            target.unlink()

    def _assert_within_root(self, target: Path) -> None:
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ContentAssetError("拒绝访问内容资产目录之外的路径") from exc
