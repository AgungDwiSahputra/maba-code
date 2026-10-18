"""Server MCP stdio kecil untuk pengujian offline (FastMCP dari paket ``mcp``)."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("echo")


@server.tool()
def echo(text: str) -> str:
    """Return the text prefixed with 'echo: '."""
    return f"echo: {text}"


@server.tool(name="weird.name")
def weird() -> str:
    """A tool whose name needs sanitizing."""
    return "weird ok"


if __name__ == "__main__":
    server.run()
