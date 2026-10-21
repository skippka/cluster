import json
import urllib.request
import urllib.error


API_VERSION = "v1"


def api_request(
    server: str,
    cluster_key: str,
    method: str,
    path: str,
    data: dict | None = None,
) -> dict:
    from .trust import NodeSession
    if isinstance(cluster_key, NodeSession):
        if server.rstrip('/') != cluster_key.server:
            raise ValueError('Credentials belong to another controller')
        return cluster_key.request(method, path, data)
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {
        "Content-Type": "application/json",
        "X-Cluster-Key": cluster_key,
    }
    request = urllib.request.Request(
        f"{server}/api/{API_VERSION}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        error.close()
        raise
