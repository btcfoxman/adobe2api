from typing import Any, List, Optional

from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1200)
    aspect_ratio: str = Field(default="16:9")
    output_resolution: str = Field(default="2K")
    model: Optional[str] = None


class TokenAddRequest(BaseModel):
    token: str


class TokenBatchAddRequest(BaseModel):
    tokens: List[str]


class ExportSelectionRequest(BaseModel):
    ids: Optional[List[str]] = None


class TokenCreditsBatchRefreshRequest(BaseModel):
    ids: Optional[List[str]] = None


class ConfigUpdateRequest(BaseModel):
    api_key: Optional[str] = None
    admin_username: Optional[str] = None
    admin_password: Optional[str] = None
    public_base_url: Optional[str] = None
    proxy: Optional[str] = None
    use_proxy: Optional[bool] = None
    generate_timeout: Optional[int] = None
    refresh_interval_hours: Optional[int] = None
    retry_enabled: Optional[bool] = None
    retry_max_attempts: Optional[int] = None
    retry_backoff_seconds: Optional[float] = None
    retry_on_status_codes: Optional[List[int]] = None
    retry_on_error_types: Optional[List[str]] = None
    token_rotation_strategy: Optional[str] = None
    batch_concurrency: Optional[int] = None
    generated_max_size_mb: Optional[int] = None
    generated_prune_size_mb: Optional[int] = None
    gpt_image_quality: Optional[str] = None
    seedance_max_concurrent: Optional[int] = None
    seedance_poll_interval_seconds: Optional[int] = None
    seedance_task_timeout_seconds: Optional[int] = None
    seedance_media_download_timeout_seconds: Optional[int] = None
    s3_enabled: Optional[bool] = None
    s3_endpoint: Optional[str] = None
    s3_region: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None
    s3_prefix: Optional[str] = None
    s3_public_base_url: Optional[str] = None
    s3_force_path_style: Optional[bool] = None
    s3_acl: Optional[str] = None


class RefreshCookieImportRequest(BaseModel):
    cookie: Any
    name: Optional[str] = None


class RefreshCookieBatchImportItem(BaseModel):
    cookie: Any
    name: Optional[str] = None


class RefreshCookieBatchImportRequest(BaseModel):
    items: List[RefreshCookieBatchImportItem]


class RefreshProfileEnabledRequest(BaseModel):
    enabled: bool


class AdminLoginRequest(BaseModel):
    username: str
    password: str
