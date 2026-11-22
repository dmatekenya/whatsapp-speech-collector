"""Generate Chichewa conversation starters for review.

Example: python -m scripts.prompt_generation.generate_starters --run-id starter-mock --mock
For a paid run, supply OPENROUTER_API_KEY through the environment.
"""

import argparse
from pathlib import Path

from src.prompt_generation.text.pool import named_directory
from src.prompt_generation.text.starters import DEFAULT_CONFIG, OUTPUT_ROOT, load_settings, run_generation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mock", action="store_true", help="use a local canned model")
    parser.add_argument("--pilot", type=int, choices=range(100, 201), metavar="100–200")
    args = parser.parse_args(argv)
    try:
        out = named_directory(OUTPUT_ROOT, args.run_id)
        run_generation(out, load_settings(args.config), mock=args.mock, pilot=args.pilot)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
