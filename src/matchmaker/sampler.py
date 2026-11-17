import random
import re
from dataclasses import dataclass

from .parser import REGEX_AVAILABLE

try:
    import regex
except ImportError:  # pragma: no cover - optional dependency
    regex = None


@dataclass
class GenerationStats:
    requested: int
    returned: int
    simplified_count: int
    main_count: int
    used_main_fallback: bool


class GraphSampler:
    def __init__(self, builder, *, validate=True):
        self.builder = builder
        self.validate = validate

    def generate_samples(self, n, seed=0):
        if n <= 0:
            return []

        rng = random.Random(seed)
        simplified = self.builder.build_simplified_graph(seed=seed)
        simplified_counts = self.compute_node_path_counts(simplified)
        simplified_total = simplified_counts.get(simplified.start_node, 0)

        seen = set()
        results = []
        if simplified_total <= n:
            simple_samples = self.generate_all(simplified, simplified_counts, limit=n, seen=seen)
        else:
            simple_samples = self.generate_stratified(
                simplified,
                n,
                seed=rng.randint(0, 2**31 - 1),
                counts=simplified_counts,
                seen=seen,
            )

        for sample in simple_samples:
            if sample not in seen and self._accept(sample):
                seen.add(sample)
                results.append(sample)
                if len(results) >= n:
                    return results

        remaining = n - len(results)
        if remaining <= 0:
            return results

        main_counts = self.compute_node_path_counts(self.builder)
        for sample in self.generate_stratified(
            self.builder,
            remaining,
            seed=rng.randint(0, 2**31 - 1),
            counts=main_counts,
            seen=seen,
            retry_multiplier=20,
        ):
            if sample not in seen and self._accept(sample):
                seen.add(sample)
                results.append(sample)
                if len(results) >= n:
                    break

        return results

    def compute_node_path_counts(self, builder):
        memo = {}
        stack = [(builder.start_node, frozenset(), 0)]
        while stack:
            node, visited, phase = stack.pop()
            if node in memo:
                continue
            if node.kind == "end":
                memo[node] = 1
                continue
            if node in visited:
                memo[node] = 0
                continue
            if phase == 0:
                stack.append((node, visited, 1))
                next_visited = visited | {node}
                for next_node in node.next:
                    if next_node not in memo:
                        stack.append((next_node, next_visited, 0))
            else:
                total = 0
                for next_node in node.next:
                    child_count = memo.get(next_node, 0)
                    if node.kind in {"match", "class", "any"}:
                        total += node.char_count * child_count
                    else:
                        total += child_count
                memo[node] = total
        return memo

    def generate_all(self, builder, counts, *, limit, seen=None):
        total = counts.get(builder.start_node, 0)
        results = []
        for index in range(min(total, limit)):
            sample = self.decode_path_index(builder, index, counts)
            if sample is None or (seen is not None and sample in seen):
                continue
            results.append(sample)
            if len(results) >= limit:
                break
        return results

    def generate_stratified(self, builder, target_count, *, seed, counts, seen=None, retry_multiplier=10):
        total = counts.get(builder.start_node, 0)
        if total <= 0 or target_count <= 0:
            return []

        rng = random.Random(seed)
        results = []
        used_indices = set()
        max_attempts = max(target_count * retry_multiplier, target_count)
        attempts = 0
        while len(results) < min(target_count, total) and attempts < max_attempts:
            attempts += 1
            index = rng.randrange(total)
            if index in used_indices:
                continue
            used_indices.add(index)
            sample = self.decode_path_index(builder, index, counts)
            if sample is None or (seen is not None and sample in seen) or sample in results:
                continue
            results.append(sample)

        if len(results) < min(target_count, total):
            bucket_count = min(target_count, total)
            for bucket_index in range(bucket_count):
                index = (bucket_index * total) // bucket_count
                if index in used_indices:
                    continue
                sample = self.decode_path_index(builder, index, counts)
                if sample is None or (seen is not None and sample in seen) or sample in results:
                    continue
                results.append(sample)
                if len(results) >= target_count:
                    break

        return results[:target_count]

    def decode_path_index(self, builder, index, counts):
        node = builder.start_node
        output = []
        memory = {}
        active_caps = {}
        steps = 0

        while node is not builder.end_node and steps < 5000:
            steps += 1
            if node.kind == "match":
                output.append(node.payload)
                node = node.next[0] if node.next else builder.end_node
            elif node.kind in {"class", "any"}:
                chars = node.payload
                if not chars:
                    return None
                downstream = sum(counts.get(next_node, 0) for next_node in node.next)
                if downstream <= 0:
                    return None
                char_index = min(index // downstream, len(chars) - 1)
                index = index % downstream
                output.append(chars[char_index])
                node = node.next[0] if node.next else builder.end_node
            elif node.kind == "split":
                next_node, index = self._choose_next(node.next, index, counts)
                if next_node is None:
                    return None
                node = next_node
            elif node.kind == "group_start":
                active_caps[node.payload] = len("".join(output))
                node = node.next[0] if node.next else builder.end_node
            elif node.kind == "group_end":
                start_index = active_caps.get(node.payload)
                if start_index is not None:
                    memory[node.payload] = "".join(output)[start_index:]
                node = node.next[0] if node.next else builder.end_node
            elif node.kind == "backref":
                if node.payload not in memory:
                    return None
                output.append(memory[node.payload])
                node = node.next[0] if node.next else builder.end_node
            elif node.kind == "conditional":
                group_captured = bool(memory.get(node.payload))
                chosen = None
                for next_node in node.next:
                    if group_captured and next_node.kind == "cond_yes":
                        chosen = next_node
                        break
                    if not group_captured and next_node.kind == "cond_no":
                        chosen = next_node
                        break
                node = chosen if chosen is not None else (node.next[0] if node.next else builder.end_node)
            elif node.kind in {"cond_yes", "cond_no", "atomic", "start"}:
                node = node.next[0] if node.next else builder.end_node
            elif node.kind == "anchor":
                next_nodes = [next_node for next_node in node.next if builder._anchor_allows(node, "".join(output), next_node)]
                if not next_nodes:
                    return None
                node = next_nodes[0]
            elif node.kind == "lookaround":
                if node.payload.get("negative"):
                    node = node.next[0] if node.next else builder.end_node
                else:
                    return None
            else:
                node = node.next[0] if node.next else builder.end_node

        if node is not builder.end_node:
            return None
        return "".join(output)

    def _choose_next(self, next_nodes, index, counts):
        for next_node in next_nodes:
            child_count = counts.get(next_node, 0)
            if index < child_count:
                return next_node, index
            index -= child_count
        return None, index

    def _accept(self, sample):
        if sample is None:
            return False
        if not self.validate:
            return True
        engine = regex if REGEX_AVAILABLE and regex is not None else re
        try:
            return engine.fullmatch(self.builder.regex, sample, self.builder.flags) is not None
        except Exception:
            return False


def generate_positive_samples(pattern, n=1000, seed=0, flags=0, validate=True):
    from .graph_builder import RegexGraphBuilder

    return RegexGraphBuilder(pattern, flags=flags, validate=validate).generate_samples(n=n, seed=seed, validate=validate)
