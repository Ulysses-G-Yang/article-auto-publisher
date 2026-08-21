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
$env:MCP_LEGACY_MUTATIONS_ENABLED = 'false'
