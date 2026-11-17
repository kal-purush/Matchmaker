from pathlib import Path
import subprocess


def _dot_escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def graph_to_dot(builder):
    lines = ["digraph regex_graph {", "  rankdir=LR;"]

    for node in builder.nodes:
        label = str(node.label)
        if node.kind in {"class", "any"} and node.char_count > 1:
            label = f"{label}\\n({node.char_count} chars)"

        shape = "box"
        color = "black"
        style = ""
        fillcolor = "white"

        if node.kind == "match":
            color = "green"
        elif node.kind in {"class", "any"}:
            color = "purple"
            style = "filled"
            fillcolor = "#f3e5f5"
        elif node.kind in {"group_start", "group_end"}:
            shape = "component"
            color = "blue"
            style = "filled"
            fillcolor = "#e3f2fd"
        elif node.kind == "backref":
            shape = "note"
            color = "orange"
            style = "filled"
            fillcolor = "#fff3e0"
        elif node.kind == "conditional":
            shape = "diamond"
            style = "filled"
            fillcolor = "#fff8e1"
        elif node.kind in {"cond_yes", "cond_no"}:
            shape = "oval"
            style = "filled"
            fillcolor = "#fff8e1"
        elif node.kind == "atomic":
            shape = "box3d"
            style = "filled"
            fillcolor = "#fce4ec"
        elif node.kind == "split":
            shape = "diamond"
        elif node.kind == "anchor":
            shape = "hexagon"
            style = "filled"
            fillcolor = "#e8f5e9"
        elif node.kind in {"start", "end"}:
            shape = "doublecircle"
            style = "filled"
            fillcolor = "#eeeeee"

        attrs = {
            "label": label,
            "shape": shape,
            "color": color,
            "fillcolor": fillcolor,
        }
        if style:
            attrs["style"] = style
        attr_text = ", ".join(f'{key}="{_dot_escape(value)}"' for key, value in attrs.items())
        lines.append(f'  "{_dot_escape(node.id)}" [{attr_text}];')

    for node in builder.nodes:
        for next_node in node.next:
            lines.append(f'  "{_dot_escape(node.id)}" -> "{_dot_escape(next_node.id)}";')

    lines.append("}")
    return "\n".join(lines) + "\n"


def write_dot(builder, path):
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(graph_to_dot(builder), encoding="utf-8")
    return output_path


def write_png(builder, path):
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import graphviz

        source = graphviz.Source(graph_to_dot(builder))
        rendered = source.render(
            filename=output_path.stem,
            directory=str(output_path.parent),
            format="png",
            cleanup=True,
        )
        rendered_path = Path(rendered)
        if rendered_path != output_path and rendered_path.exists():
            rendered_path.replace(output_path)
    except ImportError:
        dot_path = output_path.with_suffix(".dot")
        dot_path.write_text(graph_to_dot(builder), encoding="utf-8")
        try:
            subprocess.run(
                ["dot", "-Tpng", str(dot_path), "-o", str(output_path)],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("PNG rendering requires graphviz: install the Python package or the dot binary.") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"dot failed while rendering PNG: {exc.stderr.strip()}") from exc
        finally:
            dot_path.unlink(missing_ok=True)
    return output_path
