# adobe2api

---

### ✨ 广告时间 (o゜▽゜)o☆

QQ 交流群：915309828，欢迎大家一起交流。

---

Adobe Firefly / OpenAI 兼容网关服务。

English README: `README_EN.md`


当前设计：

- 对外统一入口：`/v1/chat/completions`（图像 + 视频）
- 可选图像专用接口：`/v1/images/generations`
- 持久化异步图片接口：`POST /v1/responses` + `GET /v1/responses/{response_id}`
- Token 池管理（手动 Token + 自动刷新 Token）
- 管理后台 Web UI：Token / 配置 / 日志 / 刷新配置导入

## 1）部署方式

### A. 本地开发/运行

1. **安装依赖**：

```bash
pip install -r requirements.txt
```

2. **启动服务**（在 `adobe2api/` 目录下执行）：

```bash
uvicorn app:app --host 0.0.0.0 --port 6001 --reload
```

3. **访问管理后台**：

- 地址：`http://127.0.0.1:6001/`
- 默认账号密码：`admin / admin`
- 登录后可在「系统配置」修改，或编辑 `config/config.json`

### B. Docker 部署 (推荐)

本项目已提供 Docker 支持，推荐使用 Docker Compose 一键启动：

```bash
docker compose up -d --build
```

## 2）服务鉴权

服务 API Key 配置在 `config/config.json` 的 `api_key` 字段。

- 若已设置，调用时可使用以下任一方式：
  - `Authorization: Bearer <api_key>`
  - `X-API-Key: <api_key>`

管理后台和管理 API 需要先通过 `/api/v1/auth/login` 登录并持有会话 Cookie。

## 3）外部 API 使用

### 3.0 支持的模型族

当前支持如下模型族：

- `firefly-nano-banana-*`（图像，对应上游 `nano-banana-2`）
- `firefly-nano-banana2-*`（图像，对应上游 `nano-banana-3`）
- `firefly-nano-banana-pro-*`（图像）
- `firefly-gpt-image-*`（图像，对应上游 `gpt-image:2`）
- `firefly-sora2-*`（视频）
- `firefly-sora2-pro-*`（视频）
- `firefly-veo31-*`（视频）
- `firefly-veo31-ref-*`（视频，参考图模式）
- `firefly-veo31-fast-*`（视频）
- `firefly-kling3-*`（视频，Kling 3.0，支持首尾帧参考）
- `firefly-kling-o3-*`（视频，支持实体引用）

Nano Banana 图像模型（`nano-banana-2`）：

- 命名：`firefly-nano-banana-{resolution}-{ratio}`
- 分辨率：`1k` / `2k` / `4k`
- 比例后缀：`1x1` / `16x9` / `9x16` / `4x3` / `3x4`
- 当前实现支持 `1K` / `2K` / `4K`
- 示例：
  - `firefly-nano-banana-2k-16x9`
  - `firefly-nano-banana-4k-1x1`

Nano Banana 2 图像模型（`nano-banana-3`）：

- 命名：`firefly-nano-banana2-{resolution}-{ratio}`
- 分辨率：`1k` / `2k` / `4k`
- 比例后缀：`1x1` / `16x9` / `9x16` / `4x3` / `3x4` / `1x8` / `1x4` / `4x1` / `8x1`
- Nano Banana 2 额外支持超长比例：`1:8` / `1:4` / `4:1` / `8:1`
- 当前实现支持 `1K` / `2K` / `4K`
- 示例：
  - `firefly-nano-banana2-2k-16x9`
  - `firefly-nano-banana2-4k-1x1`
  - `firefly-nano-banana2-2k-1x8`
  - `firefly-nano-banana2-2k-8x1`

Nano Banana Pro 图像模型（兼容旧命名）：

- 命名：`firefly-nano-banana-pro-{resolution}-{ratio}`
- 分辨率：`1k` / `2k` / `4k`
- 比例后缀：`1x1` / `16x9` / `9x16` / `4x3` / `3x4`
- 不包含 Nano Banana 2 的超长比例 `1:8` / `1:4` / `4:1` / `8:1`
- 当前实现支持 `1K` / `2K` / `4K`
- 示例：
  - `firefly-nano-banana-pro-2k-16x9`
  - `firefly-nano-banana-pro-4k-1x1`

GPT Image 图像模型（实验接入）：

- 命名：`firefly-gpt-image-{resolution}-{ratio}`
- 分辨率：`1k` / `2k` / `4k`
- 比例后缀：`1x1` / `5x4` / `9x16` / `21x9` / `16x9` / `4x3` / `3x2` / `4x5` / `3x4` / `2x3`
- 当前实现会携带 `outputResolution` 和对应像素 `size`
- GPT Image 质量由系统配置 `gpt_image_quality` 控制：`low` / `medium` / `high`，默认 `low`
- 示例：
  - `firefly-gpt-image-2k-16x9`
  - `firefly-gpt-image-4k-1x1`
  - `firefly-gpt-image-2k-21x9`

LiteLLM / 标准图片模型映射：

- 同步 `/v1/images/generations` 与异步 `/v1/responses` 均接受 `gpt-image-2`、`nano-banana-2`、`nano-banana-pro`，以及带 `-1k` / `-2k` / `-4k` 后缀的标准模型名。
- `model`、`size`、`ratio` / `aspect_ratio`、`resolution` 由调用方原样传入 adobe2api；实际 Firefly 模型只在 adobe2api 内部解析。
- 显式 `resolution` 优先于 `size` 推断，二者都未提供时才使用模型名的分辨率后缀；基础模型默认使用 `2k` 和 `1:1`。
- Responses 返回调用方传入的标准模型名，持久化任务内部保存解析后的 Firefly 模型名。
- 映射保存在 `config/config.json` 的 `image_model_mappings`，也可在管理后台「图片模型映射」中在线编辑。模板支持 `{resolution}`、`{ratio}`、`{ratio_colon}`、`{model}`。

关于 `auto`：

- 当前实现 **不支持** `aspect_ratio=auto`
- 如果请求里传入 `auto`，服务端会回退为 `1:1`
- 请显式传具体比例，或直接使用带比例后缀的模型 ID

Sora2 视频模型：

- 命名：`firefly-sora2-{duration}-{ratio}`
- 时长：`4s` / `8s` / `12s`
- 比例：`9x16` / `16x9`
- 示例：
  - `firefly-sora2-4s-16x9`
  - `firefly-sora2-8s-9x16`

Sora2 Pro 视频模型：

- 命名：`firefly-sora2-pro-{duration}-{ratio}`
- 时长：`4s` / `8s` / `12s`
- 比例：`9x16` / `16x9`
- 示例：
  - `firefly-sora2-pro-4s-16x9`
  - `firefly-sora2-pro-8s-9x16`

Veo31 视频模型：

- 命名：`firefly-veo31-{duration}-{ratio}-{resolution}`
- 时长：`4s` / `6s` / `8s`
- 比例：`16x9` / `9x16`
- 分辨率：`1080p` / `720p`
- 最多支持 2 张参考图：
  - 1 张：首帧参考
  - 2 张：首帧 + 尾帧参考
- 音频默认开启
- 示例：
  - `firefly-veo31-4s-16x9-1080p`
  - `firefly-veo31-6s-9x16-720p`

Veo31 Ref 视频模型（参考图模式）：

- 命名：`firefly-veo31-ref-{duration}-{ratio}-{resolution}`
- 时长：`4s` / `6s` / `8s`
- 比例：`16x9` / `9x16`
- 分辨率：`1080p` / `720p`
- 始终使用参考图模式（不是首尾帧模式）
- 最多支持 3 张参考图（映射到上游 `referenceBlobs[].usage="asset"`）
- 示例：
  - `firefly-veo31-ref-4s-9x16-720p`
  - `firefly-veo31-ref-6s-16x9-1080p`
  - `firefly-veo31-ref-8s-9x16-1080p`

Veo31 Fast 视频模型：

- 命名：`firefly-veo31-fast-{duration}-{ratio}-{resolution}`
- 时长：`4s` / `6s` / `8s`
- 比例：`16x9` / `9x16`
- 分辨率：`1080p` / `720p`
- 最多支持 2 张参考图：
  - 1 张：首帧参考
  - 2 张：首帧 + 尾帧参考
- 音频默认开启
- 示例：
  - `firefly-veo31-fast-4s-16x9-1080p`
  - `firefly-veo31-fast-6s-9x16-720p`

Kling 3.0 视频模型：

- 命名：`firefly-kling3-{duration}-{ratio}`
- 时长：`5s` / `10s` / `15s`
- 比例：`16x9` / `9x16`
- 分辨率：`720p`
- 最多支持 2 张帧参考图：1 张为首帧，2 张为首帧 + 尾帧
- 音频默认开启；可通过 `generate_audio` / `generateAudio` 覆盖
- 上游模型版本：`kling_v3_standard_i2v`
- 示例：
  - `firefly-kling3-5s-16x9`
  - `firefly-kling3-15s-9x16`

Kling O3 视频模型：

- 命名：`firefly-kling-o3-{duration}-{ratio}`
- 时长：`5s` / `15s`
- 比例：`16x9` / `9x16`
- 分辨率：`1080p`
- 最多支持 2 张帧参考图
- 支持通过 `@entity:实体名` 引用已创建的实体
- 示例：
  - `firefly-kling-o3-5s-16x9`
  - `firefly-kling-o3-15s-9x16`

### 3.1 获取模型列表

```bash
curl -X GET "http://127.0.0.1:6001/v1/models" \
  -H "Authorization: Bearer <service_api_key>"
```

### 3.2 统一入口：`/v1/chat/completions`

文生图：

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-nano-banana-pro-2k-16x9",
    "messages": [{"role":"user","content":"a cinematic mountain sunrise"}]
  }'
```

图生图（在最新 user 消息中传入图片）：

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-nano-banana-pro-2k-16x9",
    "messages": [{
      "role":"user",
      "content":[
        {"type":"text","text":"turn this photo into watercolor style"},
        {"type":"image_url","image_url":{"url":"https://example.com/input.jpg"}}
      ]
    }]
  }'
```

文生视频：

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-sora2-4s-16x9",
    "messages": [{"role":"user","content":"a drone shot over snowy forest"}]
  }'
```

Veo31 单图语义说明：

- `firefly-veo31-*` / `firefly-veo31-fast-*`：帧模式
  - 1 张图 => 首帧
  - 2 张图 => 首帧 + 尾帧
- `firefly-veo31-ref-*`：参考图模式
  - 1~3 张图 => 参考图

图生视频：

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-sora2-8s-9x16",
    "messages": [{
      "role":"user",
      "content":[
        {"type":"text","text":"animate this character walking forward"},
        {"type":"image_url","image_url":{"url":"https://example.com/character.png"}}
      ]
    }]
  }'
```

### 3.3 实体创建与可灵引用

实体用于 Kling O3 中保持角色或物体一致。实体绑定到创建它的 Adobe 账号，服务会自动获取该账号的 Creative Cloud 仓库，不需要也不支持手动配置 `repo_urn`。

创建实体：

```bash
curl -X POST "http://127.0.0.1:6001/v1/entities" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "PinkWarrior",
    "type": "object",
    "description": "A pink-haired warrior woman in futuristic armor.",
    "images": [
      "data:image/png;base64,<base64_image>"
    ]
  }'
```

字段说明：

- `name`：实体名，后续在 prompt 中使用 `@entity:name` 引用；不要包含 `@`
- `type`：`character` / `object` / `location`
- `description`：实体特征描述，最多建议 250 字符以内
- `images`：1 到 4 张图片，支持 `data:image/...;base64,...` 或纯 base64；单张最大 10MB

查看本地已绑定实体：

```bash
curl -X GET "http://127.0.0.1:6001/v1/entities" \
  -H "Authorization: Bearer <service_api_key>"
```

从当前可用 Adobe 账号同步实体列表：

```bash
curl -X GET "http://127.0.0.1:6001/v1/entities?sync=true" \
  -H "Authorization: Bearer <service_api_key>"
```

在 Kling O3 中引用实体：

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-kling-o3-5s-16x9",
    "messages": [{
      "role": "user",
      "content": "A cinematic shot of @entity:PinkWarrior walking through a neon city."
    }]
  }'
```

注意事项：

- 实体按 Adobe 账号绑定，不按 token 值绑定；token 自动刷新后仍可继续使用同一账号创建的实体
- 使用 `@entity:` 时，服务会自动切换到拥有该实体的 Adobe 账号进行生成
- 一个 prompt 中引用的多个实体必须属于同一个 Adobe 账号
- 如果多个账号存在同名实体，服务会返回歧义错误，请使用唯一实体名

### 3.4 图像接口：`/v1/images/generations`

```bash
curl -X POST "http://127.0.0.1:6001/v1/images/generations" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-nano-banana-pro-4k-16x9",
    "prompt": "futuristic city skyline at dusk"
  }'
```

### 3.5 持久化异步图片接口：`/v1/responses`

创建任务：

```bash
curl -X POST "http://127.0.0.1:6001/v1/responses" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "firefly-nano-banana-pro-2k-16x9",
    "input": "a cinematic mountain sunrise",
    "tools": [{"type":"image_generation"}],
    "background": true
  }'
```

也可以使用标准模型名并独立传入尺寸、比例和分辨率：

```bash
curl -X POST "http://127.0.0.1:6001/v1/images/generations" \
  -H "Authorization: Bearer projectx_webapp" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2-4k",
    "prompt": "a cinematic city skyline",
    "size": "2048x1152",
    "ratio": "16:9",
    "resolution": "2k"
  }'
```

返回的 `id` 为 `resp_*`。使用该 ID 查询任务：

```bash
curl "http://127.0.0.1:6001/v1/responses/<response_id>" \
  -H "Authorization: Bearer <service_api_key>"
```

状态依次为 `in_progress`、`completed` 或 `failed`；成功后图片地址位于
`output[].url`。任务存储在 `data/response_tasks.db`，已完成任务在服务重启后仍可查询；
重启时尚未完成的任务会标记为 `failed`。`input` 中的 `input_image` 可用于异步图生图。

## 4）Cookie 导入

### 第一步：使用浏览器插件导出（推荐）

本项目提供了一个配套的浏览器插件，可以方便地从 Adobe Firefly 页面导出所需的 Cookie 数据。

- 插件源码位置：`browser-cookie-exporter/`
- 可导出最简 `cookie_*.json`（仅包含 `cookie` 字段）
- 详细说明见：`browser-cookie-exporter/README.md`

**重要提示：建议优先使用无痕窗口导出 Cookie。**

- 同一个浏览器的普通窗口里，如果你连续登录多个 Adobe 账号并反复导出，后一次登录通常会把前一次账号的 Cookie 顶掉
- 结果就是：你前面导出的 Cookie 可能很快失效，导入后表现为刷新失败、账号掉线，或只能保留最后一次导出的账号
- **最稳妥的做法：每个账号都在单独的无痕窗口中登录并导出，再分别导入**

**插件安装与使用步骤：**

1. 打开 Chrome 或 Edge 浏览器的扩展管理页：`chrome://extensions`
2. 开启右上角的「开发者模式」
3. 点击「加载已解压的扩展程序」，选择项目中的 `browser-cookie-exporter/` 目录
4. 打开插件详情页，开启「允许在无痕模式下运行」
5. 为每个 Adobe 账号分别打开一个新的无痕窗口
6. 在对应的无痕窗口中登录 [Adobe Firefly](https://firefly.adobe.com/)
7. 点击浏览器工具栏的插件图标，选择导出范围
8. 点击「导出最简 JSON」并保存文件

### 第二步：导入到项目中

拿到导出的 JSON 文件后，按照以下流程导入服务：

1. 访问并登录管理后台（默认 `http://127.0.0.1:6001/`）
2. 打开「Token 管理」页签
3. 点击「导入 Cookie」按钮
4. **方式 A：** 粘贴 JSON 文件内容到文本框；**方式 B：** 直接上传导出的 `.json` 文件
5. 点击「确认导入」（服务会自动验证 Cookie 并执行一次刷新）
6. 导入成功后，Token 列表中会显示对应的 Token，且 `自动刷新` 状态为「是」

**批量导入：** 导入弹窗支持一次上传多个文件，或粘贴包含多个账户信息的 JSON 数组。

## 5）存储路径

- 生成媒体文件：`data/generated/`
- 异步图片任务数据库：`data/response_tasks.db`
- 请求日志：`data/request_logs.jsonl`
- Token 池：`config/tokens.json`
- 服务配置：`config/config.json`
- 刷新配置（本地私有）：`config/refresh_profile.json`

生成媒体保留策略：

- `data/generated/` 下文件会保留，并通过 `/generated/*` 对外访问
- 启用按容量阈值自动清理（最旧文件优先）
  - `generated_max_size_mb`（默认 `1024`）
  - `generated_prune_size_mb`（默认 `200`）
- 当总大小超过 `generated_max_size_mb` 时，服务会删除旧文件，直到至少回收 `generated_prune_size_mb`且总大小降回阈值以内

## Seedance standard async API

- Create: `POST /api/v3/contents/generations/tasks`
- Query: `GET /api/v3/contents/generations/tasks/{task_id}`
- Models: `doubao-seedance-2-0-fast-260128`, `doubao-seedance-2-0-260128`
- Create response provider: `firefly`
- Duration: `4` to `15` seconds
- Ratio: `16:9` / `9:16` / `3:4` / `4:3` / `1:1`
- Resolution: `720p`
- Media links: up to 9 images, 1 video, and 3 audios
- Default mode: omni reference (`generationType=3` / `mode=reference`)
- First/end frame mode: pass `generationType=2` or `mode=i2v_first_last`; use 1 or 2 images, submitted as Firefly `referenceBlobs` with `usage=frame` and `order=1/2`
- Query responses omit `helper_task_id` and `provider`.
- Optional S3 upload: enable `s3_enabled` and configure `s3_endpoint`, `s3_region`, `s3_bucket`, `s3_access_key`, `s3_secret_key`, `s3_prefix`, `s3_public_base_url`, `s3_force_path_style`, and `s3_acl` in admin settings. When enabled, `video_url` is the uploaded S3 public URL.

```bash
curl -X POST "http://127.0.0.1:6001/api/v3/contents/generations/tasks" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "doubao-seedance-2-0-fast-260128",
    "prompt": "Create a video with @Image1 and @Audio1.",
    "duration": 8,
    "ratio": "16:9",
    "resolution": "720p",
    "image_urls": ["https://example.com/input.png"],
    "audio_urls": ["https://example.com/voice.mp3"]
  }'
```

First/end frame example:

```bash
curl -X POST "http://127.0.0.1:6001/api/v3/contents/generations/tasks" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "doubao-seedance-2-0-260128",
    "prompt": "Create a smooth transition from image 1 to image 2.",
    "duration": 5,
    "ratio": "16:9",
    "resolution": "720p",
    "generationType": 2,
    "images": [
      "https://example.com/first-frame.png",
      "https://example.com/last-frame.png"
    ]
  }'
```

Queued/running responses keep `video_url` and `content.video_url` empty. Succeeded responses return the same final video URL in top-level `video_url`, `content.video_url`, and `items[0].video_url`.

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=leik1000/adobe2api&type=Date)](https://star-history.com/#leik1000/adobe2api&Date)
