from rich.console import Console

from simple_eval.cli import app
from simple_eval.errors import SimpleEvalError


def main() -> None:
    try:
        app()
    except SimpleEvalError as error:
        Console(stderr=True).print(f"[red]error:[/red] {error}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
