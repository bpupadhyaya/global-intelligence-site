// GISimulation — the Global Simulation page's force-directed graph renderer.
//
// REBUILD (2026-08-24): v1 rendered the graph on top of the bounded SVG world map (a fixed
// 960x500 viewport, only the 133 supply-chain paths). That undersold the mission -- this
// version is an unbounded, pannable/zoomable Canvas diagram (positions come from graph
// structure via d3-force, not a map projection) carrying all 591 nodes: the physical
// supply-chain backbone (chokepoints/ports, real lat/lng, PINNED so they stay geographically
// correct), every one of the 397 Human Endeavor items and their 20 category hubs, and the
// financial/logical layer -- all wired into the same graph. Real chokepoints/ports keep a
// pinned, correct position; everything else is laid out by what it connects to, and the graph
// is free to sprawl arbitrarily far in any direction, panned/zoomed like the Linux-distro
// timeline this page's design was modeled on. The world map is drawn faded, in the same
// coordinate space, purely as an orientation reference for the pinned nodes -- it is not the
// layout mechanism.
//
// Requires d3.v7, topojson-client.v3, and world-map.js already loaded on the page.
(function (global) {
    function esc(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // Distance-attenuated propagation: direct neighbors get full impact, each further hop is
    // weaker. Simple, explainable, and easy to justify/adjust -- not a black-box score. See
    // docs/global-simulation/README.md (private repo) Phase 6 for the reasoning.
    var HOP_DECAY = 0.5;
    var MAX_HOPS = 6;

    var NODE_COLOR = {
        backbone: '#168158',
        category_hub: '#1d4ed8',
        item: '#7c8a83',
        financial: '#b45309'
    };
    var EDGE_COLOR = {
        supply_chain: '#8fa398',
        structural: '#c7cdc9' // item_hub / item_link / hub_backbone / financial / item_financial / cross_layer
    };

    function nodeGroup(n) {
        if (n.type === 'category_hub') return 'category_hub';
        if (n.type === 'item') return 'item';
        if ((n.type || '').indexOf('financial_') === 0) return 'financial';
        return 'backbone';
    }

    function init(config) {
        var map, canvas, ctx, dpr, wrap;
        var data = null; // {nodes, edges, item_links}
        var nodeById = {}, edgeById = {}, edgesByNodeId = {}; // adjacency for propagation
        var sim, forceLinks;
        var disabled = {}; // id -> true, for both node and edge ids
        var affected = {}; // id -> hop distance (0 = disabled itself), across nodes+edges
        var transform = d3.zoomIdentity;
        var hoveredId = null, selectedId = null;
        var zoomBehavior;
        var geoPathGen = null;
        var fitted = false;

        function buildAdjacency() {
            nodeById = {};
            data.nodes.forEach(function (n) { nodeById[n.id] = n; });
            edgeById = {};
            data.edges.forEach(function (e) { edgeById[e.id] = e; });
            edgesByNodeId = {};
            data.edges.forEach(function (e) {
                (e.nodes || []).forEach(function (nid) {
                    (edgesByNodeId[nid] = edgesByNodeId[nid] || []).push(e);
                });
            });
        }

        var countryLngLatCache = {};
        function resolveNodeLngLat(n) {
            if (n.lat !== null && n.lat !== undefined && n.lng !== null && n.lng !== undefined) {
                return [n.lng, n.lat];
            }
            if (n.country) {
                if (!(n.country in countryLngLatCache)) countryLngLatCache[n.country] = map.countryLngLat(n.country);
                return countryLngLatCache[n.country];
            }
            return null;
        }

        // Pin every node with a real geographic anchor at its projected map position; every
        // other node (items, category hubs, financial entities) is seeded near the map's
        // center and left free for the force simulation to place based on what it connects to.
        function pinGeoNodes() {
            data.nodes.forEach(function (n) {
                var lngLat = resolveNodeLngLat(n);
                if (lngLat) {
                    var p = map.project(lngLat);
                    if (p) { n.fx = p[0]; n.fy = p[1]; n.x = p[0]; n.y = p[1]; return; }
                }
                n.x = map.width / 2 + (Math.random() - 0.5) * map.width * 0.6;
                n.y = map.height / 2 + (Math.random() - 0.5) * map.height * 0.6;
            });
        }

        // d3-force's forceLink wants {source, target} pairs; our edges carry a `nodes` array
        // (2 for most edge types, more for a few multi-chokepoint supply-chain paths) -- chain
        // consecutive pairs so multi-node edges still pull together without a combinatorial
        // blow-up. This array only drives layout; rendering still reads the original edges.
        function buildForceLinks() {
            var links = [];
            data.edges.forEach(function (e) {
                var ids = e.nodes || [];
                for (var i = 0; i < ids.length - 1; i++) {
                    links.push({ source: ids[i], target: ids[i + 1], edgeType: e.edge_type || 'supply_chain' });
                }
            });
            return links;
        }

        var LINK_DISTANCE = { supply_chain: 34, hub_backbone: 38, item_hub: 16, item_link: 40, item_financial: 46, financial: 40, cross_layer: 65 };
        var LINK_STRENGTH = { supply_chain: 0.25, hub_backbone: 0.09, item_hub: 0.5, item_link: 0.14, item_financial: 0.15, financial: 0.3, cross_layer: 0.15 };
        var COLLIDE_RADIUS = { category_hub: 15, item: 5, financial: 7, backbone: 6 };

        function startSimulation() {
            forceLinks = buildForceLinks();
            sim = d3.forceSimulation(data.nodes)
                .force('link', d3.forceLink(forceLinks).id(function (d) { return d.id; })
                    .distance(function (l) { return LINK_DISTANCE[l.edgeType] || 40; })
                    .strength(function (l) { return LINK_STRENGTH[l.edgeType] || 0.1; }))
                .force('charge', d3.forceManyBody().strength(-26).distanceMax(500))
                .force('collide', d3.forceCollide().radius(function (d) { return COLLIDE_RADIUS[nodeGroup(d)] || 6; }))
                .alphaDecay(0.02)
                .on('tick', draw)
                .on('end', function () { fitToBounds(); draw(); });
        }

        // Deliberately does NOT fit the whole graph into view -- showing everything at once on
        // load proves the diagram is bounded, which is exactly the "infinite diagram" feel this
        // page is supposed to have. Instead centers on the pinned physical backbone at a normal
        // reading zoom, so category-hub/item/financial constellations sit mostly off-screen and
        // have to be discovered by panning outward -- the diagram keeps going past the frame.
        function fitToBounds() {
            if (fitted || !data.nodes.length) return;
            fitted = true;
            var anchors = data.nodes.filter(function (n) { return n.fx !== undefined; });
            var pts = anchors.length ? anchors : data.nodes;
            var xs = pts.map(function (n) { return n.x; });
            var ys = pts.map(function (n) { return n.y; });
            var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
            var minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
            var w = Math.max(1, maxX - minX), h = Math.max(1, maxY - minY);
            var rect = wrap.getBoundingClientRect();
            var k = Math.max(0.3, Math.min(3, 0.75 * Math.min(rect.width / w, rect.height / h)));
            var cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
            var t = d3.zoomIdentity.translate(rect.width / 2 - cx * k, rect.height / 2 - cy * k).scale(k);
            d3.select(canvas).call(zoomBehavior.transform, t);
        }

        function resizeCanvas() {
            var rect = wrap.getBoundingClientRect();
            dpr = window.devicePixelRatio || 1;
            canvas.width = Math.round(rect.width * dpr);
            canvas.height = Math.round(rect.height * dpr);
            canvas.style.width = rect.width + 'px';
            canvas.style.height = rect.height + 'px';
            draw();
        }

        function draw() {
            if (!data) return;
            ctx.save();
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, canvas.width / dpr, canvas.height / dpr);
            ctx.translate(transform.x, transform.y);
            ctx.scale(transform.k, transform.k);

            // Faded world map -- orientation reference only, drawn in the same coordinate
            // space as the pinned backbone nodes so they line up with their real location.
            if (geoPathGen) {
                ctx.beginPath();
                geoPathGen(map.countries);
                ctx.fillStyle = 'rgba(22, 129, 88, 0.06)';
                ctx.fill();
                ctx.lineWidth = 0.6 / transform.k;
                ctx.strokeStyle = 'rgba(22, 129, 88, 0.16)';
                ctx.stroke();
            }

            // Edges first (under nodes)
            data.edges.forEach(function (e) {
                var pts = (e.nodes || []).map(function (nid) { return nodeById[nid]; }).filter(Boolean);
                if (pts.length < 1) return;
                var isDisabled = !!disabled[e.id];
                var hop = affected[e.id];
                var isHovered = e.id === hoveredId || e.id === selectedId;
                var base = e.edge_type === 'supply_chain' ? EDGE_COLOR.supply_chain : EDGE_COLOR.structural;
                var color = isDisabled ? '#dc2626' : (hop !== undefined ? '#8a5a06' : isHovered ? '#12211b' : base);
                var baseAlpha = e.edge_type === 'supply_chain' ? 0.35 : e.edge_type === 'hub_backbone' ? 0.22 : 0.15;
                var alpha = isDisabled ? 1 : (hop !== undefined ? Math.max(0.25, 1 - hop * HOP_DECAY) : isHovered ? 0.9 : baseAlpha);
                ctx.strokeStyle = color;
                ctx.globalAlpha = alpha;
                ctx.lineWidth = (isDisabled ? 2.5 : hop !== undefined ? 1.8 : isHovered ? 2 : 0.8) / transform.k;
                if (pts.length === 1) {
                    ctx.beginPath();
                    ctx.arc(pts[0].x, pts[0].y, 6 / transform.k, 0, Math.PI * 2);
                    ctx.stroke();
                } else {
                    ctx.beginPath();
                    ctx.moveTo(pts[0].x, pts[0].y);
                    for (var i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
                    ctx.stroke();
                }
                ctx.globalAlpha = 1;
            });

            // Nodes on top
            data.nodes.forEach(function (n) {
                if (n.x === undefined || n.y === undefined) return;
                var isDisabled = !!disabled[n.id];
                var hop = affected[n.id];
                var group = nodeGroup(n);
                var baseR = group === 'category_hub' ? 6 : group === 'item' ? 2.6 : group === 'financial' ? 4 : 4;
                var r = (isDisabled ? baseR + 2.5 : n.id === selectedId ? baseR + 1.5 : baseR) / transform.k;
                var color = isDisabled ? '#dc2626' : hop !== undefined ? '#8a5a06' : NODE_COLOR[group];
                ctx.beginPath();
                ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
                ctx.fillStyle = color;
                ctx.globalAlpha = isDisabled ? 1 : hop !== undefined ? Math.max(0.35, 1 - hop * HOP_DECAY) : (group === 'item' ? 0.55 : 0.85);
                ctx.fill();
                if (n.id === hoveredId || n.id === selectedId) {
                    ctx.lineWidth = 1.5 / transform.k;
                    ctx.strokeStyle = '#12211b';
                    ctx.globalAlpha = 1;
                    ctx.stroke();
                }
                ctx.globalAlpha = 1;
            });
            ctx.restore();
        }

        function distToSegment(px, py, ax, ay, bx, by) {
            var dx = bx - ax, dy = by - ay;
            var len2 = dx * dx + dy * dy;
            var t = len2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2)) : 0;
            var cx = ax + t * dx, cy = ay + t * dy;
            return Math.hypot(px - cx, py - cy);
        }

        function hitTest(clientX, clientY) {
            var rect = canvas.getBoundingClientRect();
            var localX = (clientX - rect.left - transform.x) / transform.k;
            var localY = (clientY - rect.top - transform.y) / transform.k;
            var best = null, bestDist = 14 / transform.k;
            data.nodes.forEach(function (n) {
                if (n.x === undefined || n.y === undefined) return;
                var d = Math.hypot(n.x - localX, n.y - localY);
                if (d < bestDist) { bestDist = d; best = { type: 'node', id: n.id }; }
            });
            if (best) return best;
            // No node close enough -- check edges (distance to nearest segment of each polyline).
            var edgeBestDist = 6 / transform.k;
            data.edges.forEach(function (e) {
                var pts = (e.nodes || []).map(function (nid) { return nodeById[nid]; }).filter(Boolean);
                for (var i = 0; i < pts.length - 1; i++) {
                    var d = distToSegment(localX, localY, pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y);
                    if (d < edgeBestDist) { edgeBestDist = d; best = { type: 'edge', id: e.id }; }
                }
            });
            return best;
        }

        function propagate() {
            affected = {};
            var disabledIds = Object.keys(disabled).filter(function (id) { return disabled[id]; });
            if (!disabledIds.length) { renderAffectedItems(); draw(); return; }
            // BFS over the node/edge adjacency graph from every disabled entity.
            var queue = disabledIds.map(function (id) { return { id: id, hop: 0 }; });
            var visited = {};
            disabledIds.forEach(function (id) { visited[id] = true; });
            while (queue.length) {
                var cur = queue.shift();
                if (cur.hop > 0 && affected[cur.id] === undefined) affected[cur.id] = cur.hop;
                if (cur.hop >= MAX_HOPS) continue;
                var neighborEdges = nodeById[cur.id] ? (edgesByNodeId[cur.id] || []) : data.edges.filter(function (e) { return e.id === cur.id; });
                neighborEdges.forEach(function (e) {
                    if (!visited[e.id]) { visited[e.id] = true; queue.push({ id: e.id, hop: cur.hop + 1 }); }
                    (e.nodes || []).forEach(function (nid) {
                        if (!visited[nid]) { visited[nid] = true; queue.push({ id: nid, hop: cur.hop + 1 }); }
                    });
                });
            }
            renderAffectedItems();
            draw();
        }

        function toggleDisabled(id) {
            disabled[id] = !disabled[id];
            propagate();
            renderDetail(id);
            // Guarantee the detail/impact panel is actually visible the moment something is
            // disabled -- on narrow viewports it starts collapsed behind a toggle button, and a
            // silent state change on a dense canvas is otherwise easy to miss entirely.
            if (config.panelId) {
                var panel = document.getElementById(config.panelId);
                if (panel && !panel.classList.contains('open')) {
                    panel.classList.add('open');
                    if (config.panelToggleId) {
                        var toggleBtn = document.getElementById(config.panelToggleId);
                        if (toggleBtn) {
                            toggleBtn.setAttribute('aria-expanded', 'true');
                            toggleBtn.textContent = 'Search & detail ▴';
                        }
                    }
                }
                panel.scrollIntoView && panel.scrollIntoView({ block: 'nearest' });
            }
        }

        function renderDetail(id) {
            var el = document.getElementById(config.detailId);
            var n = nodeById[id];
            var e = !n ? edgeById[id] : null;
            if (!n && !e) { el.innerHTML = ''; return; }
            selectedId = id;
            var isDis = !!disabled[id];
            var html;
            if (n) {
                var metaBits = [n.type || 'node'];
                if (n.lat !== null && n.lat !== undefined) metaBits.push(n.lat.toFixed(2) + ', ' + n.lng.toFixed(2));
                else if (n.country) metaBits.push(n.country);
                html =
                    '<h3>' + esc(n.name) + '</h3>' +
                    '<div class="meta">' + metaBits.map(esc).join(' · ') + '</div>' +
                    (n.vulnerability_factors && n.vulnerability_factors.length
                        ? '<p class="note">' + n.vulnerability_factors.map(function (v) { return esc(v.text); }).join(' ') + '</p>'
                        : '') +
                    (n.type === 'item' && n.page ? '<p class="note"><a href="' + esc(n.page) + '">View item page →</a></p>' : '');
            } else {
                var edgeMetaBits = [(e.edge_type || 'connection').replace(/_/g, ' ')];
                if (e.category) edgeMetaBits.push(e.category);
                if (e.estimated_global_share) edgeMetaBits.push(e.estimated_global_share);
                html =
                    '<h3>' + esc(e.name) + '</h3>' +
                    '<div class="meta">' + edgeMetaBits.map(esc).join(' · ') + '</div>' +
                    (e.commodities && e.commodities.length
                        ? '<p class="note">Commodities: ' + esc(e.commodities.join(', ')) + '</p>' : '') +
                    (e.match_basis ? '<p class="note">' + esc(e.match_basis) + '</p>' : '') +
                    (e.sources && e.sources.length
                        ? '<p class="note">Source: <a href="' + esc(e.sources[0]) + '">' + esc(e.sources[0]) + '</a></p>' : '');
            }
            el.innerHTML = '<div class="sim-node-detail">' + html +
                    '<button class="' + (isDis ? 'restore' : '') + '" id="sim-toggle-btn">' + (isDis ? 'Restore' : 'Disable') + '</button>' +
                '</div>';
            document.getElementById('sim-toggle-btn').addEventListener('click', function () { toggleDisabled(id); });
        }

        function renderNodeList() {
            var el = document.getElementById(config.nodeListId);
            el.innerHTML = data.nodes.map(function (n) {
                return '<div class="map-path-row" data-id="' + esc(n.id) + '">' +
                    '<div class="mprow-name">' + esc(n.name) + '</div>' +
                    '<div class="mprow-meta">' + esc(n.type || '') + '</div>' +
                '</div>';
            }).join('');
            el.querySelectorAll('.map-path-row').forEach(function (row) {
                row.addEventListener('click', function () { renderDetail(row.dataset.id); draw(); });
                row.addEventListener('mouseenter', function () { hoveredId = row.dataset.id; draw(); });
                row.addEventListener('mouseleave', function () { hoveredId = null; draw(); });
            });
        }

        function renderAffectedItems() {
            var el = document.getElementById(config.affectedItemsId);
            var wrapEl = config.affectedWrapId ? document.getElementById(config.affectedWrapId) : null;
            var anyDisabled = Object.keys(disabled).some(function (k) { return disabled[k]; });
            if (!anyDisabled) {
                el.innerHTML = '';
                if (wrapEl) wrapEl.hidden = true;
                return;
            }
            var rows = [];
            (data.item_links || []).forEach(function (link) {
                var minHop = null;
                (link.linked_edges || []).forEach(function (eid) {
                    var h = disabled[eid] ? 0 : affected[eid];
                    if (h !== undefined && (minHop === null || h < minHop)) minHop = h;
                });
                if (minHop !== null) rows.push({ link: link, hop: minHop });
            });
            // Also surface items whose own node (or category hub) was reached via the graph,
            // not just the legacy direct commodity-string item_links list.
            data.nodes.forEach(function (n) {
                if (n.type !== 'item') return;
                var h = disabled[n.id] ? 0 : affected[n.id];
                if (h === undefined) return;
                if (rows.some(function (r) { return r.link.item_id === n.id.replace(/^item-[^-]+-/, ''); })) return;
                rows.push({ link: { item_name: n.name, item_page: n.page, category: n.category_id, item_page_full: n.page }, hop: h, isGraphNode: true });
            });
            rows.sort(function (a, b) { return a.hop - b.hop; });
            if (wrapEl) wrapEl.hidden = false;
            if (!rows.length) {
                el.innerHTML = '<p class="mprow-meta">No linked items reachable from what\'s currently disabled.</p>';
                return;
            }
            el.innerHTML = rows.slice(0, 60).map(function (r) {
                var page = r.link.item_page || r.link.item_page_full || '#';
                return '<a class="map-path-row" style="display:block; text-decoration:none;" href="' + esc(page) + '">' +
                    '<div class="mprow-name">' + esc(r.link.item_name) + '</div>' +
                    '<div class="mprow-meta">' + esc(r.link.category || '') + ' · ' + (r.hop === 0 ? 'directly disabled' : r.hop + ' hop' + (r.hop > 1 ? 's' : '') + ' away') + '</div>' +
                '</a>';
            }).join('');
        }

        function nameOf(hit) {
            if (!hit) return '';
            var obj = hit.type === 'node' ? nodeById[hit.id] : edgeById[hit.id];
            return obj ? obj.name : '';
        }

        function setupInteraction() {
            var tooltipEl = config.tooltipId ? document.getElementById(config.tooltipId) : null;

            zoomBehavior = d3.zoom().scaleExtent([0.04, 24]).on('zoom', function (event) {
                transform = event.transform;
                if (tooltipEl) tooltipEl.hidden = true;
                draw();
            });
            d3.select(canvas).call(zoomBehavior);

            canvas.addEventListener('click', function (event) {
                var hit = hitTest(event.clientX, event.clientY);
                if (hit) { toggleDisabled(hit.id); }
            });
            canvas.addEventListener('mousemove', function (event) {
                var hit = hitTest(event.clientX, event.clientY);
                var newHover = hit ? hit.id : null;
                if (newHover !== hoveredId) { hoveredId = newHover; draw(); }
                canvas.style.cursor = hit ? 'pointer' : 'grab';
                if (tooltipEl) {
                    if (hit) {
                        var rect = canvas.getBoundingClientRect();
                        tooltipEl.textContent = nameOf(hit);
                        tooltipEl.style.left = (event.clientX - rect.left) + 'px';
                        tooltipEl.style.top = (event.clientY - rect.top) + 'px';
                        tooltipEl.hidden = false;
                    } else {
                        tooltipEl.hidden = true;
                    }
                }
            });
            canvas.addEventListener('mouseleave', function () {
                if (tooltipEl) tooltipEl.hidden = true;
            });
        }

        function setupSearch() {
            document.getElementById(config.searchId).addEventListener('input', function (e) {
                var q = e.target.value.trim().toLowerCase();
                document.querySelectorAll('#' + config.nodeListId + ' .map-path-row').forEach(function (row) {
                    var name = row.querySelector('.mprow-name').textContent.toLowerCase();
                    row.style.display = !q || name.indexOf(q) !== -1 ? '' : 'none';
                });
            });
        }

        Promise.all([
            fetch(config.atlasUrl).then(function (r) { return r.json(); }),
            fetch(config.graphUrl + '?t=' + Date.now()).then(function (r) { return r.json(); })
        ]).then(function (results) {
            var atlas = results[0];
            data = results[1];
            map = global.GIMap.init(null, atlas);
            geoPathGen = d3.geoPath(map.projection);
            canvas = document.getElementById(config.canvasId);
            wrap = document.querySelector(config.canvasWrapSelector);
            ctx = canvas.getContext('2d');
            buildAdjacency();
            pinGeoNodes();
            resizeCanvas();
            window.addEventListener('resize', resizeCanvas);
            setupInteraction();
            setupSearch();
            renderNodeList();
            renderAffectedItems();
            startSimulation();
            geoPathGen.context(ctx);

            var itemCount = data.nodes.filter(function (n) { return n.type === 'item'; }).length;
            var stampEl = document.getElementById(config.stampId);
            stampEl.textContent = data.nodes.length + ' nodes (' + itemCount + ' items), ' + data.edges.length + ' connections' +
                (data.generated ? ' · updated ' + data.generated : '');
            document.getElementById(config.listHeadId).textContent = data.nodes.length + ' nodes';
        }).catch(function (err) {
            document.getElementById(config.stampId).textContent = 'Graph data unavailable';
            console.error('GISimulation load error:', err);
        });
    }

    global.GISimulation = { init: init };
})(window);
