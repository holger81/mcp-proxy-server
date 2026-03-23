import uvicorn

from mcp_proxy.settings import Settings


def main() -> None:
    s = Settings()
    uvicorn.run(
        "mcp_proxy.app:app",
        host=s.host,
        port=s.port,
        factory=False,
    )


if __name__ == "__main__":
    main()
