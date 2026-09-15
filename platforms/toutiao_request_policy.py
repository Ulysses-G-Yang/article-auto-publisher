"""Exact native-page exceptions to the existing-publication write guard.

These endpoints were observed in the native page's requests and public client.
They do not grant permission to upload images or change account/editor settings.
Never infer that a new POST is safe merely from its HTTP method or host.
"""

from urllib.parse import unquote, urlsplit

NATIVE_BACKGROUND_POSTS = {
    ("mssdk.bytedance.com", "/web/r/token"): "SESSION_PROTOCOL",
    ("mssdk.bytedance.com", "/web/common"): "SESSION_PROTOCOL",
    ("mp.toutiao.com", "/ttwid/check/"): "SESSION_PROTOCOL",
    ("mp.toutiao.com", "/bcs/notice/boxes/"): "NOTIFICATION_READ",
    ("mp.toutiao.com", "/mp/agw/mass_profit/jingxuan_account_check"): "ELIGIBILITY_READ",
    ("abtestvm.bytedance.com", "/service/2/abtest_config/"): "PAGE_CONFIGURATION",
    ("mcs.zijieapi.com", "/list"): "NATIVE_TELEMETRY",
    ("mp.toutiao.com", "/monitor_browser/collect/batch/"): "NATIVE_TELEMETRY",
    ("mon.zijieapi.com", "/monitor_browser/collect/batch/"): "NATIVE_TELEMETRY",
    ("security.zijieapi.com", "/api/metrics/emit"): "NATIVE_TELEMETRY",
    ("mp.toutiao.com", "/syltrack/api/report/v1"): "NATIVE_TELEMETRY",
    (
        "firebaseinstallations.googleapis.com",
        "/v1/projects/byted-ucenter/installations",
    ): "SESSION_PROTOCOL",
}


def publication_request_kind(method: str, url: str) -> str:
    """Classify only; never constructs a request or fabricates a success reply."""
    parsed = urlsplit(url)
    path = unquote(parsed.path)
    if path == "/mp/agw/article/publish":
        if method == "POST" and parsed.scheme == "https" and parsed.netloc == "mp.toutiao.com":
            return "ARTICLE_SUBMISSION"
        return "FORBIDDEN_MUTATION"
    if path == "/mp/agw/article/new" or any(
        word in path.lower() for word in ("delete", "post_now")
    ):
        return "FORBIDDEN_MUTATION"
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "READ"
    if (
        method == "POST"
        and parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and (parsed.netloc, parsed.path) in NATIVE_BACKGROUND_POSTS
    ):
        return NATIVE_BACKGROUND_POSTS[parsed.netloc, parsed.path]
    if parsed.netloc == "mp.toutiao.com" and path == "/spice/image":
        return "IMAGE_UPLOAD_REQUIRED"
    return "UNRECOGNIZED_WRITE"
