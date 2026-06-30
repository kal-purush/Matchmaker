import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from regex_positive_generator import RegexGraphBuilder
from regex_positive_generator.visualization import write_dot, write_png


def main():
    parser = argparse.ArgumentParser(description="Render a regex graph as DOT and optionally PNG.")
    parser.add_argument("pattern", nargs="?", default=r"(cat|dog)\d{1,2}")
    parser.add_argument("--output-dir", default=str(Path(__file__).resolve().parent / "outputs"))
    parser.add_argument("--name", default="sample_regex_graph")
    parser.add_argument("--png", action="store_true")
    args = parser.parse_args()

    builder = RegexGraphBuilder(args.pattern, validate=False)
    output_dir = Path(args.output_dir)
    dot_path = write_dot(builder, output_dir / f"{args.name}.dot")
    print(f"Wrote DOT: {dot_path}")
    print(f"Nodes: {len(builder.nodes)}")
    print(f"Edges: {sum(len(node.next) for node in builder.nodes)}")

    if args.png:
        png_path = write_png(builder, output_dir / f"{args.name}.png")
        print(f"Wrote PNG: {png_path}")


if __name__ == "__main__":
    main()
