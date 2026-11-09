"""Static board info: geometry for the viewer and text for the agents."""

import math

from catanatron.models.map import Port, NodeRef, PORT_DIRECTION_TO_NODEREFS
from catanatron.models.board import STATIC_GRAPH

PIPS = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 8: 5, 9: 4, 10: 3, 11: 2, 12: 1}
SIZE = 60.0  # hex radius in viewer pixels
SQ3 = math.sqrt(3)
NODE_OFFSETS = {
    NodeRef.NORTH: (0, -1),
    NodeRef.NORTHEAST: (SQ3 / 2, -0.5),
    NodeRef.SOUTHEAST: (SQ3 / 2, 0.5),
    NodeRef.SOUTH: (0, 1),
    NodeRef.SOUTHWEST: (-SQ3 / 2, 0.5),
    NodeRef.NORTHWEST: (-SQ3 / 2, -0.5),
}


def hex_center(coord):
    """Cube coord -> pixel centre (pointy-top hexes)."""
    x, _y, z = coord
    return (SIZE * SQ3 * (x + z / 2), SIZE * 1.5 * z)


class BoardInfo:
    def __init__(self, catan_map):
        self.map = catan_map
        self.tiles = {}  # tile id -> dict
        self.node_xy = {}
        self.node_tiles = {}  # node -> [tile id]
        for coord, t in catan_map.land_tiles.items():
            cx, cy = hex_center(coord)
            self.tiles[t.id] = {
                "id": t.id,
                "resource": t.resource or "DESERT",
                "number": t.number,
                "pips": PIPS.get(t.number, 0),
                "x": round(cx, 2),
                "y": round(cy, 2),
                "nodes": [t.nodes[ref] for ref in NodeRef],
            }
            for ref, node in t.nodes.items():
                ox, oy = NODE_OFFSETS[ref]
                self.node_xy[node] = (round(cx + ox * SIZE, 2), round(cy + oy * SIZE, 2))
                self.node_tiles.setdefault(node, []).append(t.id)
        for n in self.node_tiles:
            self.node_tiles[n].sort()

        self.ports = []
        self.node_port = {}
        for coord, t in catan_map.tiles.items():
            if not isinstance(t, Port):
                continue
            a, b = PORT_DIRECTION_TO_NODEREFS[t.direction]
            nodes = [t.nodes[a], t.nodes[b]]
            label = f"{t.resource} 2:1" if t.resource else "3:1"
            cx, cy = hex_center(coord)
            self.ports.append({"x": round(cx, 2), "y": round(cy, 2), "nodes": nodes,
                               "resource": t.resource, "label": label})
            for n in nodes:
                self.node_port[n] = label

        land = set(self.node_xy)
        self.node_neighbors = {
            n: sorted(m for m in STATIC_GRAPH.neighbors(n) if m in land) for n in land
        }
        self.edges = sorted({tuple(sorted(e)) for e in STATIC_GRAPH.edges() if e[0] in land and e[1] in land})
        self.coord_to_tile = {coord: t.id for coord, t in catan_map.land_tiles.items()}

    # ------------------------------------------------------------ text helpers
    def tile_label(self, tid):
        t = self.tiles[tid]
        if t["resource"] == "DESERT":
            return f"T{tid} DESERT"
        return f"T{tid} {t['resource']} {t['number']}"

    def node_production(self, node):
        parts, pips = [], 0
        for tid in self.node_tiles[node]:
            t = self.tiles[tid]
            if t["resource"] == "DESERT":
                parts.append("DESERT")
            else:
                parts.append(f"{t['resource']}{t['number']}")
                pips += t["pips"]
        return parts, pips

    def node_summary(self, node):
        parts, pips = self.node_production(node)
        port = self.node_port.get(node)
        s = f"N{node} [{' + '.join(parts)} = {pips} pips]"
        if port:
            s += f" (port {port})"
        return s

    def static_text(self):
        lines = ["TILES (id: resource number, pips; corner nodes clockwise from north):"]
        for tid in sorted(self.tiles):
            t = self.tiles[tid]
            corners = ",".join(str(n) for n in t["nodes"])
            if t["resource"] == "DESERT":
                lines.append(f"  T{tid}: DESERT; nodes {corners}")
            else:
                lines.append(f"  T{tid}: {t['resource']} {t['number']}, {t['pips']} pips; nodes {corners}")
        lines.append("")
        lines.append("PORTS:")
        for p in self.ports:
            lines.append(f"  {p['label']} at nodes {p['nodes'][0]} and {p['nodes'][1]}")
        lines.append("")
        lines.append("NODES (id: adjacent tiles = total pips | port | neighbouring nodes):")
        for n in sorted(self.node_xy):
            parts, pips = self.node_production(n)
            port = self.node_port.get(n, "-")
            nb = " ".join(f"N{m}" for m in self.node_neighbors[n])
            lines.append(f"  N{n}: {' + '.join(parts)} = {pips} | {port} | {nb}")
        return "\n".join(lines)

    def to_json(self):
        return {
            "tiles": list(self.tiles.values()),
            "nodes": {str(n): xy for n, xy in self.node_xy.items()},
            "ports": self.ports,
            "edges": self.edges,
            "size": SIZE,
        }
