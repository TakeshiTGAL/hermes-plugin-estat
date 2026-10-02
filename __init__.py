"""e-Stat plugin for Hermes Agent: official Japanese government statistics.

Three read-only tools that take an agent from a question to a cited number:
search tables, inspect a table's dimensions, and fetch values by name.
"""

# Hermes imports this directory as a package. pytest's collector also imports this
# file on its own (the repo root is the package), where relative imports cannot work.
if __package__:
    from . import client, schemas, tools


def register(ctx):
    """Register every tool under the `estat` toolset."""
    for schema in schemas.ALL_SCHEMAS:
        ctx.register_tool(
            name=schema["name"],
            toolset="estat",
            schema=schema,
            handler=tools.HANDLERS[schema["name"]],
            requires_env=[client.APP_ID_ENV],
            emoji="📊",
        )
