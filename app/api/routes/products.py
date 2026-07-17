"""产品库 API(V8.0)。

提供产品 CRUD 和 CSV 批量导入。
数据存储在 SQLite products 表中。
"""

from __future__ import annotations

import csv
import io
import logging
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import StreamingResponse

from app.api.routes.auth import get_current_user
from app.core.database import delete_product, list_products, upsert_product

logger = logging.getLogger(__name__)

# V17.0: 产品库全员共享, 但需登录才能访问
router = APIRouter(
    prefix="/api/products",
    tags=["products"],
    dependencies=[Depends(get_current_user)],
)


@router.get("")
@router.get("/")
async def get_products():
    """获取所有产品列表。"""
    return list_products()


@router.post("")
@router.post("/")
async def create_product(
    name: str = Form(..., description="产品名称"),
    selling_points: str = Form("", description="核心卖点(逗号分隔)"),
    image_urls: str = Form("", description="参考图 URL(逗号分隔)"),
):
    """新增单个产品。"""
    if not name.strip():
        raise HTTPException(status_code=400, detail="产品名称不能为空")

    urls = [u.strip() for u in image_urls.split(",") if u.strip()]
    pid = upsert_product(
        name=name.strip(),
        selling_points=selling_points.strip(),
        image_urls=urls,
    )
    return {"id": pid, "name": name.strip(), "message": "产品已创建"}


@router.delete("/{product_id}")
async def remove_product(product_id: str):
    """删除产品。"""
    if not delete_product(product_id):
        raise HTTPException(status_code=404, detail="产品不存在")
    return {"message": "产品已删除", "id": product_id}


@router.post("/import-csv")
async def import_products_csv(
    csv_file: UploadFile = File(..., description="CSV 文件"),
):
    """CSV 批量导入产品(兼容 UTF-8 BOM 和 GBK)。

    CSV 格式:
        name,selling_points,image_urls
        产品A,卖点1,卖点2,https://example.com/a.png,https://example.com/b.png
    """
    if not csv_file.filename or not csv_file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="请上传 .csv 文件")

    raw = await csv_file.read()

    # 尝试多种编码(UTF-8 BOM → UTF-8 → GBK)
    text: Optional[str] = None
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb2312"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if text is None:
        raise HTTPException(status_code=400, detail="无法解码 CSV 文件(请使用 UTF-8 或 GBK 编码)")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "name" not in [f.strip().lower() for f in reader.fieldnames]:
        raise HTTPException(
            status_code=400,
            detail="CSV 必须包含 name 列(可选: selling_points, image_urls)",
        )

    imported = 0
    errors: list[str] = []
    for i, row in enumerate(reader, start=2):
        # 标准化列名(去除空格,小写)
        normalized = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        name = normalized.get("name", "")
        if not name:
            errors.append(f"第{i}行: 产品名称为空,跳过")
            continue

        selling_points = normalized.get("selling_points", "")
        image_urls_str = normalized.get("image_urls", "")
        urls = [u.strip() for u in image_urls_str.split(",") if u.strip()]

        try:
            upsert_product(name=name, selling_points=selling_points, image_urls=urls)
            imported += 1
        except Exception as exc:
            errors.append(f"第{i}行: {exc}")

    logger.info("[Products] CSV 导入完成: %d 个产品, %d 个错误", imported, len(errors))
    return {
        "imported": imported,
        "errors": errors,
        "total_rows": imported + len(errors),
    }


@router.get("/template")
async def download_csv_template():
    """下载 CSV 导入模板。"""
    csv_content = "name,selling_points,image_urls\n"
    csv_content += "Premium Tumbler,24h insulation,316 stainless,leak-proof,https://example.com/product.png\n"
    csv_content += "Wireless Earbuds,ANC noise cancelling,30h battery,IPX5 waterproof,https://example.com/earbuds.png\n"

    return StreamingResponse(
        io.BytesIO(csv_content.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=products_template.csv"},
    )
