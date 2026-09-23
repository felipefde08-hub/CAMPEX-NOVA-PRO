from starlette.middleware.cors import CORSMiddleware


class LocalNetworkCORSMiddleware(CORSMiddleware):
    """Allow hosted frontend access to a local runtime, only for allowed origins."""

    def __init__(self, app, *, local_runtime=False, **kwargs):
        super().__init__(app, **kwargs)
        self.local_runtime = local_runtime
        self.allow_private_network = local_runtime

    def preflight_response(self, request_headers):
        response = super().preflight_response(request_headers)
        if (
            self.local_runtime
            and response.status_code == 200
            and request_headers.get("access-control-request-private-network") == "true"
            and self.is_allowed_origin(request_headers.get("origin", ""))
        ):
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        elif "access-control-allow-private-network" in response.headers:
            del response.headers["access-control-allow-private-network"]
        return response
