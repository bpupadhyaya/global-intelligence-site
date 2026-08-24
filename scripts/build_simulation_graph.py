#!/usr/bin/env python3
"""Build the Global Simulation graph (data/simulation/graph.json).

MOVED HERE FROM pvt/global-intelligence (2026-08-24). The original design kept this pipeline in
the private repo and pushed only the computed output across a repo boundary via a scoped PAT --
correct in principle, but GitHub Actions' default GITHUB_TOKEN can never write to a different
repo no matter the permissions settings (a hard platform restriction, not a config gap), so that
design required a manually-created fine-grained PAT before it could run unattended. Bhim chose to
drop that requirement and run this exactly like every other self-refreshing pipeline on this site
(metal_prices.yml, dashboard_taxonomy.yml, etc.): script + source data + workflow all live in this
repo, which already has `contents: write` on itself via the default token -- no secret needed.

Tradeoff, stated plainly: the linking/classification methodology (data/simulation_src/) is no
longer private. Only the raw material the script actually reads made the move -- see
data/simulation_src/ vs. what's still private-only below.

Inputs (this repo):
  data/simulation_src/nodes_physical.json         -- canonical physical chokepoint/port nodes
  data/simulation_src/nodes_virtual_country.json  -- country-level origin nodes for concentration-risk paths
  data/simulation_src/nodes_financial.json        -- financial-layer nodes (Phase 7)
  data/simulation_src/edges_financial.json        -- financial-layer edges (Phase 7)
  data/simulation_src/raw_label_to_node_id.json   -- maps every raw chokepoint string to its canonical node id
  data/simulation_src/vulnerability_factors_raw.json -- abstract risk-factor entries (not map nodes)
  data/simulation_src/item_links.json             -- item-to-path direct commodity-string matches (Phase 3)
  data/simulation_src/item_links_extended.json    -- item-to-path genuine conceptual matches (optional, if present)
  data/supply_chain_paths.json                    -- the 133 curated paths
  data/supply_chain_geo.json                      -- resolved origin_countries per path
  data/dashboard/taxonomy_structure.json          -- 397 items / 20 categories

Still private-only (pvt/global-intelligence/data/simulation/), not needed to RUN this script,
kept there as research history: chokepoint_classifications_raw.json, edges.json,
item_unmatched.json, financial_layer_research.md.

Output: data/simulation/graph.json

Run: python3 scripts/build_simulation_graph.py
"""
import datetime
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIM_SRC = ROOT / "data" / "simulation_src"
SIM_OUT = ROOT / "data" / "simulation"

# Real-world domain correspondence between taxonomy categories and supply-chain path
# categories -- both are independently-curated real category systems, this table only states
# where they cover the same real-world domain, it invents no new facts.
HUB_TO_SC_CATEGORIES = {
    "energy": ["energy", "coal", "renewable-energy-equipment"],
    "commodities": ["minerals-metals"],
    "food": ["agriculture"],
    "healthcare": ["pharmaceuticals-medical-supplies"],
    "chemicals-fertilizers": ["fertilizer", "chemicals-plastics"],
    "manufacturing": ["industrial"],
}

# Direct, specific item-to-financial-node matches -- only made where an item IS the real-world
# thing a financial node represents (not a loose thematic association).
FINANCE_ITEM_LINKS = {
    "foreign-exchange": ["fin-currency-usd", "fin-currency-eur", "fin-currency-jpy",
                          "fin-currency-gbp", "fin-currency-cny", "fin-currency-chf"],
    "central-banking": ["fin-cb-fed", "fin-cb-ecb", "fin-cb-boj", "fin-cb-boe", "fin-cb-snb", "fin-cb-boc"],
    "equity-markets": ["fin-exchange-nasdaq", "fin-exchange-nyse", "fin-exchange-shanghai",
                        "fin-exchange-euronext", "fin-exchange-jpx", "fin-exchange-shenzhen",
                        "fin-exchange-hkex", "fin-exchange-bse", "fin-exchange-nse", "fin-exchange-tsx"],
    "sovereign-wealth-funds": ["fin-swf-norway", "fin-swf-safe", "fin-swf-cic", "fin-swf-adia",
                                "fin-swf-kia", "fin-swf-gic", "fin-swf-pif"],
    "payments-infrastructure": ["fin-rail-swift", "fin-rail-cls", "fin-rail-fedwire",
                                 "fin-rail-chips", "fin-rail-target2"],
    "remittances": ["fin-rail-swift"],
}

# Gulf oil-revenue SWFs are literally capitalized by oil exports through Hormuz -- a real,
# specific, sourced cross-layer fact, not an inference.
GULF_OIL_SWF_IDS = ["fin-swf-adia", "fin-swf-pif", "fin-swf-kia"]
GULF_OIL_PATH_ID = "persian-gulf-strait-of-hormuz"


def slugify(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


def load(name):
    return json.loads((SIM_SRC / name).read_text())


def load_optional(name):
    p = SIM_SRC / name
    return json.loads(p.read_text()) if p.exists() else []


def build_edges(raw_to_node_id, abstract_by_raw, paths_data, geo_by_id):
    edges = []
    unmatched_warnings = []
    for p in paths_data["paths"]:
        node_ids = []
        vuln_factors = []
        for cp in p.get("chokepoints", []):
            if cp in raw_to_node_id:
                nid = raw_to_node_id[cp]
                if nid not in node_ids:
                    node_ids.append(nid)
            elif cp in abstract_by_raw:
                a = abstract_by_raw[cp]
                vuln_factors.append({"text": cp, "risk_category": a.get("risk_category", "other")})
            else:
                unmatched_warnings.append((p["id"], cp))

        # Concentration-risk paths with zero physical chokepoint nodes fall back to a
        # country-level origin node (see nodes_virtual_country.json) rather than being
        # invisible on the map -- their real bottleneck is corporate/geographic concentration,
        # not a transit point, but they still deserve a place in the graph.
        node_is_virtual = False
        if not node_ids:
            geo = geo_by_id.get(p["id"])
            if geo and geo.get("origin_countries"):
                first_country = geo["origin_countries"][0]
                node_ids = ["country-" + slugify(first_country)]
                node_is_virtual = True

        edges.append({
            "id": p["id"],
            "name": p["name"],
            "category": p.get("category"),
            "nodes": node_ids,
            "node_is_virtual_country": node_is_virtual,
            "commodities": p.get("commodities", []),
            "vulnerability_factors": vuln_factors,
            "estimated_global_share": p.get("estimated_global_share"),
            "sources": p.get("sources", []),
            "site_path_anchor": p["id"],
        })

    if unmatched_warnings:
        print(f"WARNING: {len(unmatched_warnings)} chokepoint strings matched neither a node "
              f"nor a known risk factor -- likely supply_chain_paths.json changed since "
              f"data/simulation_src/*.json was last regenerated. Re-run the classification pass "
              f"(see pvt/global-intelligence docs/global-simulation/README.md Phase 1) before "
              f"trusting this output.")
        for path_id, cp in unmatched_warnings[:10]:
            print(f"  {path_id}: {cp!r}")

    return edges


def build_hub_and_item_layer(taxonomy, paths_by_category, item_matches_by_id):
    """Category-hub nodes (20) + item nodes (397) + their edges into the backbone.

    item_matches_by_id: (category_id, item_id) -> {"linked_paths": [...], "match_basis": str}
    merged from item_links.json (direct commodity-string matches) and item_links_extended.json
    (genuine conceptual matches), if present.
    """
    hub_nodes, item_nodes, hub_edges, item_hub_edges, item_link_edges = [], [], [], [], []

    for cat in taxonomy["root"]["children"]:
        hub_id = f"hub-{cat['id']}"
        hub_nodes.append({"id": hub_id, "name": cat["name"], "type": "category_hub", "category_id": cat["id"]})

        for item in cat.get("children", []):
            item_id = f"item-{cat['id']}-{item['id']}"
            item_nodes.append({
                "id": item_id, "name": item["name"], "type": "item",
                "category_id": cat["id"], "page": item.get("page"),
            })
            item_hub_edges.append({
                "id": f"item-hub-{cat['id']}-{item['id']}", "name": f"{item['name']} is part of {cat['name']}",
                "edge_type": "item_hub", "category": cat["id"], "nodes": [item_id, hub_id],
                "sources": [],
            })

            match = item_matches_by_id.get((cat["id"], item["id"]))
            if match:
                for path_id in match["linked_paths"]:
                    for node_id in paths_by_category["nodes_by_path_id"].get(path_id, []):
                        item_link_edges.append({
                            "id": f"item-link-{cat['id']}-{item['id']}-{node_id}",
                            "name": f"{item['name']} <- {paths_by_category['path_name_by_id'].get(path_id, path_id)}",
                            "edge_type": "item_link", "category": cat["id"], "nodes": [item_id, node_id],
                            "sources": [], "match_basis": match["match_basis"],
                        })

    for cat in taxonomy["root"]["children"]:
        hub_id = f"hub-{cat['id']}"
        sc_cats = HUB_TO_SC_CATEGORIES.get(cat["id"], [])
        if not sc_cats:
            continue
        touched = set()
        for path_id, sc_cat in paths_by_category["path_category_by_id"].items():
            if sc_cat in sc_cats:
                touched.update(paths_by_category["nodes_by_path_id"].get(path_id, []))
        for node_id in sorted(touched):
            hub_edges.append({
                "id": f"hub-backbone-{cat['id']}-{node_id}", "name": f"{cat['name']} <-> {node_id}",
                "edge_type": "hub_backbone", "category": cat["id"], "nodes": [hub_id, node_id],
                "sources": [],
            })

    return hub_nodes, item_nodes, hub_edges + item_hub_edges + item_link_edges


def build_financial_layer(nodes_by_path_id):
    fin_nodes = load("nodes_financial.json")
    fin_edges_raw = load("edges_financial.json")
    fin_edges = [{
        "id": e["id"], "name": e["name"], "edge_type": "financial", "category": e["category"],
        "nodes": e["nodes"], "sources": e.get("sources", []),
    } for e in fin_edges_raw]

    item_financial_edges = []
    for item_id, fin_ids in FINANCE_ITEM_LINKS.items():
        item_node_id = f"item-finance-{item_id}"
        for fin_id in fin_ids:
            item_financial_edges.append({
                "id": f"item-financial-{item_id}-{fin_id}", "name": f"{item_id} <-> {fin_id}",
                "edge_type": "item_financial", "category": "finance", "nodes": [item_node_id, fin_id],
                "sources": [],
            })

    cross_layer_edges = []
    for node_id in nodes_by_path_id.get(GULF_OIL_PATH_ID, []):
        for swf_id in GULF_OIL_SWF_IDS:
            cross_layer_edges.append({
                "id": f"cross-layer-{swf_id}-{node_id}",
                "name": f"{swf_id} is capitalized by Gulf oil exports through {node_id}",
                "edge_type": "cross_layer", "category": "finance", "nodes": [swf_id, node_id],
                "sources": ["https://www.adia.ae/", "https://www.pif.gov.sa/", "https://www.kia.gov.kw/"],
            })

    return fin_nodes, fin_edges + item_financial_edges + cross_layer_edges


def main():
    physical_nodes = load("nodes_physical.json")
    virtual_nodes = load("nodes_virtual_country.json")
    raw_to_node_id = load("raw_label_to_node_id.json")
    abstract_entries = load("vulnerability_factors_raw.json")
    abstract_by_raw = {e["raw"]: e for e in abstract_entries}
    item_matches = load("item_links.json") + load_optional("item_links_extended.json")
    item_matches_by_id = {(m["category_id"], m["id"]): m for m in item_matches}

    paths_data = json.loads((ROOT / "data" / "supply_chain_paths.json").read_text())
    geo_data = json.loads((ROOT / "data" / "supply_chain_geo.json").read_text())
    geo_by_id = {p["id"]: p for p in geo_data["paths"]}
    taxonomy = json.loads((ROOT / "data" / "dashboard" / "taxonomy_structure.json").read_text())

    edges = build_edges(raw_to_node_id, abstract_by_raw, paths_data, geo_by_id)

    used_node_ids = set()
    for e in edges:
        used_node_ids.update(e["nodes"])

    backbone_nodes = []
    for n in physical_nodes + virtual_nodes:
        if n["id"] not in used_node_ids:
            continue
        node = {"id": n["id"], "name": n["name"], "type": n["type"], "lat": n.get("lat"), "lng": n.get("lng")}
        if n.get("country"):
            node["country"] = n["country"]
        backbone_nodes.append(node)

    backbone_edges = [{
        "id": e["id"], "name": e["name"], "edge_type": "supply_chain", "category": e["category"], "nodes": e["nodes"],
        "commodities": e["commodities"], "vulnerability_factors": e["vulnerability_factors"],
        "estimated_global_share": e.get("estimated_global_share"), "sources": e.get("sources", []),
        "site_path_anchor": e["site_path_anchor"],
    } for e in edges]

    nodes_by_path_id = {e["site_path_anchor"]: e["nodes"] for e in backbone_edges}
    path_category_by_id = {p["id"]: p.get("category") for p in paths_data["paths"]}
    path_name_by_id = {p["id"]: p["name"] for p in paths_data["paths"]}
    paths_by_category = {
        "nodes_by_path_id": nodes_by_path_id,
        "path_category_by_id": path_category_by_id,
        "path_name_by_id": path_name_by_id,
    }

    hub_nodes, item_nodes, human_endeavor_edges = build_hub_and_item_layer(
        taxonomy, paths_by_category, item_matches_by_id)
    financial_nodes, financial_layer_edges = build_financial_layer(nodes_by_path_id)

    final_nodes = backbone_nodes + hub_nodes + item_nodes + financial_nodes
    final_edges = backbone_edges + human_endeavor_edges + financial_layer_edges

    final_item_links = [{
        "item_id": m["id"], "item_name": m["name"], "item_page": m["page"], "category": m["category"],
        "linked_edges": m["linked_paths"], "match_basis": m["match_basis"],
    } for m in item_matches]

    note_text = (
        "Full graph: a real physical supply-chain backbone (chokepoints/ports with a verified "
        "lat/lng, or a country-level origin node where a path's bottleneck is corporate/geographic "
        "concentration rather than a transit point) connected to all 397 Human Endeavor items via "
        "their 20 real taxonomy categories -- each item always links to its own category; each "
        "category links onward into the backbone wherever a real domain correspondence exists; a "
        "handful of items link directly to specific real financial-layer entities (FX, central "
        "banking, exchanges, sovereign wealth funds, payments rails). Physical/virtual nodes carry a "
        "real map position; item/hub/financial nodes do not -- they are laid out by the graph "
        "structure itself, not geography. Never a fabricated coordinate or a forced link -- "
        "unresolvable locations and connections are honestly absent rather than guessed."
    )

    graph = {
        "schema_version": "2.0.0",
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_from": "os/global-intelligence-site scripts/build_simulation_graph.py",
        "note": note_text,
        "nodes": final_nodes,
        "edges": final_edges,
        "item_links": final_item_links,
    }

    SIM_OUT.mkdir(parents=True, exist_ok=True)
    out_path = SIM_OUT / "graph.json"
    out_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False) + "\n")

    print(f"Wrote {out_path}")
    print(f"  nodes: {len(final_nodes)} (backbone={len(backbone_nodes)} hub={len(hub_nodes)} "
          f"item={len(item_nodes)} financial={len(financial_nodes)})")
    print(f"  edges: {len(final_edges)} (backbone={len(backbone_edges)} human_endeavor={len(human_endeavor_edges)} "
          f"financial={len(financial_layer_edges)})")
    print(f"  item_links: {len(final_item_links)}")


if __name__ == "__main__":
    main()
