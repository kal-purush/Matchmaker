class Node:
    def __init__(self, label, kind="simple", payload=None):
        self.id = None
        self.label = label
        self.kind = kind
        self.payload = payload
        self.next = []
        self.char_count = 1

    def connect(self, node):
        self.next.append(node)
        return node

    def __repr__(self):
        return f"[{self.label}]"
