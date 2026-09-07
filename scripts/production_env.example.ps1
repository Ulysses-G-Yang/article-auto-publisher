# ArticleOps Windows production configuration template.
# Copy this file to data\production_env.ps1, replace the secret and network
# allowlist, then dot-source it before starting the Waitress/MCP processes.

$env:APP_ENV = 'production'
$env:APP_SECRET_KEY = 'REPLACE_WITH_A_RANDOM_SECRET_AT_LEAST_32_CHARACTERS'
$env:APP_DEBUG = 'false'
$env:FLASK_HOST = '127.0.0.1'
$env:FLASK_PORT = '5000'
$env:FLASK_BASE_URL = 'http://127.0.0.1:5000'

# Keep public publishing and the legacy queue disabled for the RC.
$env:PUBLISH_AFTER_DRAFT = 'false'
$env:LEGACY_UPLOAD_QUEUE_ENABLED = 'false'
$env:ACCOUNT_SESSIONS_ALLOW_PUBLIC_PUBLISH = 'false'

# Runtime state is created beside this config file; never point this at a
# source checkout containing real data, Cookie, or Profile directories.
$env:APP_DATA_DIR = (Join-Path $PSScriptRoot '.')

$env:MCP_BIND_HOST = '127.0.0.1'
$env:MCP_PORT = '8765'
$env:MCP_ALLOWED_HOSTS = '127.0.0.1,localhost'
$env:MCP_FILE_SERVICE_ALLOWED_HOSTS = 'dev.sccsai.com'
$env:ARTICLEOPS_MCP_INTERNAL_TOKEN = 'REPLACE_WITH_A_SEPARATE_RANDOM_SECRET_AT_LEAST_32_CHARACTERS'
# 登录账号后，把获准由 CS_Admin 使用的 account_id 以逗号分隔填入；不得使用 *。
$env:ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS = '00000000-0000-0000-0000-000000000000'
# 只有完成账号白名单配置后才改为 true；该开关仅授予平台草稿权限。
$env:ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED = 'false'
$env:MCP_LEGACY_MUTATIONS_ENABLED = 'false'

# 可选的只读平台建议配置。以下内容在示例中保持注释；管理员可按需维护到
# data\production_env.ps1。
# 实际启用时将启用开关设为 'true'；示例中的 'false' 只是安全占位。
# $env:ARTICLEOPS_AI_GUIDANCE_ENABLED = 'false'
# $env:DEEPSEEK_BASE_URL = 'https://api.deepseek.com'
# $env:DEEPSEEK_MODEL = 'deepseek-v4-flash'
# $env:DEEPSEEK_API_KEY = '<set only in the administrator-maintained local file>'
# 可选的超时和输出限制（均有默认值）：
# $env:DEEPSEEK_CONNECT_TIMEOUT_SECONDS = '10'
# $env:DEEPSEEK_READ_TIMEOUT_SECONDS = '90'
# $env:DEEPSEEK_MAX_OUTPUT_TOKENS = '2000'
