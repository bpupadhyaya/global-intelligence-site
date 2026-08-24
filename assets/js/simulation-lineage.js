// GISimulationLineage — a second, completely independent renderer for the Global Simulation
// graph, modeled directly on the GNU/Linux-distribution-lineage timeline chart: fixed
// horizontal lanes (one per Human Endeavor category, plus one for the financial layer),
// left-to-right generational columns (physical backbone -> category -> item/financial
// entity), and an unbounded Canvas -- no map, no force simulation, no shared code with
// simulation.js/world-map.js. Node positions are computed once, deterministically, from the
// graph's own real structure (never randomized, never fabricated); the diagram has no edge in
// either direction, matching the "keeps going" reference chart this page is modeled on.
(function (global) {
    function esc(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    var HOP_DECAY = 0.5;
    var MAX_HOPS = 6;

    // Real-world domain correspondence between supply-chain path categories and the taxonomy
    // lane they belong to -- same table the build pipeline uses for hub_backbone edges, kept
    // in sync by hand since this is a read-only client-side consumer of the same graph.json.
    var SC_TO_LANE = {
        energy: 'energy', coal: 'energy', 'renewable-energy-equipment': 'energy',
        'minerals-metals': 'commodities',
        agriculture: 'food',
        'pharmaceuticals-medical-supplies': 'healthcare',
        fertilizer: 'chemicals-fertilizers', 'chemicals-plastics': 'chemicals-fertilizers',
        industrial: 'manufacturing'
    };

    // The site's own 20 Human Endeavor categories, in their established display order (see
    // dashboard/taxonomy_structure.json), plus one extra lane for the financial/logical layer,
    // placed right after Finance for visual proximity.
    var LANE_ORDER = [
        'energy', 'food', 'water', 'healthcare', 'finance', 'financial', 'commodities',
        'housing-construction', 'technology', 'transportation', 'supply-chain', 'manufacturing',
        'chemicals-fertilizers', 'telecommunications', 'education', 'defense-security',
        'textiles-apparel', 'retail', 'real-estate', 'entertainment-media', 'space-aerospace'
    ];
    var LANE_LABEL = {
        energy: 'Energy', food: 'Food & Agriculture', water: 'Water', healthcare: 'Healthcare & Medicine',
        finance: 'Finance & Capital Markets', financial: 'Financial / Logical Layer',
        commodities: 'Metals & Mining', 'housing-construction': 'Housing & Construction',
        technology: 'Technology & Semiconductors', transportation: 'Transportation & Logistics',
        'supply-chain': 'Supply Chain & Trade Routes', manufacturing: 'Manufacturing & Industrial Goods',
        'chemicals-fertilizers': 'Chemicals & Fertilizers', telecommunications: 'Telecommunications',
        education: 'Education', 'defense-security': 'Defense & Security', 'textiles-apparel': 'Textiles & Apparel',
        retail: 'Retail & Consumer Goods', 'real-estate': 'Real Estate & Property Markets',
        'entertainment-media': 'Entertainment & Media', 'space-aerospace': 'Space & Aerospace'
    };

    var COL_X = [0, 480, 960]; // backbone, category hub, item/financial-entity
    var NODE_SPACING = 17;
    var LANE_PADDING = 34;
    var LANE_GAP = 10;

    var NODE_COLOR = { backbone: '#168158', category_hub: '#1d4ed8', item: '#7c8a83', financial: '#b45309' };
    var EDGE_COLOR = { supply_chain: '#8fa398', structural: '#c7cdc9' };

    function nodeGroup(n) {
        if (n.type === 'category_hub') return 'category_hub';
        if (n.type === 'item') return 'item';
        if ((n.type || '').indexOf('financial_') === 0) return 'financial';
        return 'backbone';
    }

    function init(config) {
        var canvas, ctx, dpr, wrap;
        var data = null;
        var nodeById = {}, edgeById = {}, edgesByNodeId = {};
        var laneTop = {}, laneHeight = {};
        var disabled = {}, affected = {};
        var transform = d3.zoomIdentity;
        var hoveredId = null, selectedId = null;
        var zoomBehavior;

        function laneOfBackbone(n) {
            var edges = edgesByNodeId[n.id] || [];
            var counts = {};
            edges.forEach(function (e) {
                if (e.edge_type !== 'supply_chain') return;
                var lane = SC_TO_LANE[e.category];
                if (lane) counts[lane] = (counts[lane] || 0) + 1;
            });
            var best = null, bestCount = 0;
            Object.keys(counts).forEach(function (l) { if (counts[l] > bestCount) { best = l; bestCount = counts[l]; } });
            return best || 'supply-chain';
        }

        function laneOf(n) {
            var g = nodeGroup(n);
            if (g === 'category_hub' || g === 'item') return n.category_id;
            if (g === 'financial') return 'financial';
            return laneOfBackbone(n);
        }

        function computeLayout() {
            // Bucket every node into {lane, col} -> [] first (needs edgesByNodeId, so backbone
            // lane assignment happens after adjacency is built).
            var buckets = {};
            data.nodes.forEach(function (n) {
                var lane = laneOf(n);
                var col = nodeGroup(n) === 'category_hub' ? 1 : nodeGroup(n) === 'backbone' ? 0 : 2;
                var key = lane + '|' + col;
                (buckets[key] = buckets[key] || []).push(n);
            });
            Object.keys(buckets).forEach(function (key) {
                buckets[key].sort(function (a, b) { return a.name.localeCompare(b.name); });
            });

            var y = 40;
            LANE_ORDER.forEach(function (lane) {
                var maxStack = Math.max(
                    (buckets[lane + '|0'] || []).length,
                    (buckets[lane + '|1'] || []).length,
                    (buckets[lane + '|2'] || []).length,
                    1
                );
                var h = maxStack * NODE_SPACING + LANE_PADDING * 2;
                laneTop[lane] = y;
                laneHeight[lane] = h;
                [0, 1, 2].forEach(function (col) {
                    var list = buckets[lane + '|' + col] || [];
                    list.forEach(function (n, i) {
                        n.x = COL_X[col];
                        n.y = y + LANE_PADDING + i * NODE_SPACING;
                    });
                });
                y += h + LANE_GAP;
            });
        }

        function buildAdjacency() {
            nodeById = {}; edgeById = {}; edgesByNodeId = {};
            data.nodes.forEach(function (n) { nodeById[n.id] = n; });
            data.edges.forEach(function (e) {
                edgeById[e.id] = e;
                (e.nodes || []).forEach(function (nid) { (edgesByNodeId[nid] = edgesByNodeId[nid] || []).push(e); });
            });
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
            var rect = wrap.getBoundingClientRect();
            ctx.save();
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, canvas.width / dpr, canvas.height / dpr);
            ctx.translate(transform.x, transform.y);
            ctx.scale(transform.k, transform.k);

            // Lane bands + labels -- the "totally new" background: no map, just the lineage
            // chart's own structure (alternating tint bands, one per Human Endeavor category).
            var viewLeft = -transform.x / transform.k, viewRight = (rect.width - transform.x) / transform.k;
            LANE_ORDER.forEach(function (lane, i) {
                var top = laneTop[lane], h = laneHeight[lane];
                ctx.fillStyle = i % 2 === 0 ? 'rgba(22,129,88,0.03)' : 'rgba(22,129,88,0.00)';
                ctx.fillRect(viewLeft, top, viewRight - viewLeft, h);
                ctx.fillStyle = 'rgba(20,40,32,0.38)';
                ctx.font = (12 / transform.k) + 'px Inter, sans-serif';
                ctx.textBaseline = 'top';
                ctx.fillText(LANE_LABEL[lane] || lane, viewLeft + 6 / transform.k, top + 4 / transform.k);
            });

            // Edges as gentle flowing curves (classic lineage-chart look), under nodes.
            data.edges.forEach(function (e) {
                var pts = (e.nodes || []).map(function (nid) { return nodeById[nid]; }).filter(Boolean);
                if (pts.length < 1) return;
                var isDisabled = !!disabled[e.id];
                var hop = affected[e.id];
                var isHovered = e.id === hoveredId || e.id === selectedId;
                var base = e.edge_type === 'supply_chain' ? EDGE_COLOR.supply_chain : EDGE_COLOR.structural;
                var color = isDisabled ? '#dc2626' : (hop !== undefined ? '#8a5a06' : isHovered ? '#12211b' : base);
                var baseAlpha = e.edge_type === 'supply_chain' ? 0.4 : e.edge_type === 'hub_backbone' ? 0.22 : 0.16;
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
                    for (var i = 1; i < pts.length; i++) {
                        var a = pts[i - 1], b = pts[i];
                        var midX = (a.x + b.x) / 2;
                        ctx.bezierCurveTo(midX, a.y, midX, b.y, b.x, b.y);
                    }
                    ctx.stroke();
                }
                ctx.globalAlpha = 1;
            });

            // Nodes on top.
            data.nodes.forEach(function (n) {
                if (n.x === undefined) return;
                var isDisabled = !!disabled[n.id];
                var hop = affected[n.id];
                var group = nodeGroup(n);
                var baseR = group === 'category_hub' ? 6 : group === 'item' ? 3 : group === 'financial' ? 4.5 : 4.5;
                var r = (isDisabled ? baseR + 2.5 : n.id === selectedId ? baseR + 1.5 : baseR) / transform.k;
                var color = isDisabled ? '#dc2626' : hop !== undefined ? '#8a5a06' : NODE_COLOR[group];
                ctx.beginPath();
                ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
                ctx.fillStyle = color;
                ctx.globalAlpha = isDisabled ? 1 : hop !== undefined ? Math.max(0.35, 1 - hop * HOP_DECAY) : (group === 'item' ? 0.7 : 0.9);
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
                if (n.x === undefined) return;
                var d = Math.hypot(n.x - localX, n.y - localY);
                if (d < bestDist) { bestDist = d; best = { type: 'node', id: n.id }; }
            });
            if (best) return best;
            var edgeBestDist = 6 / transform.k;
            data.edges.forEach(function (e) {
                var pts = (e.nodes || []).map(function (nid) { return nodeById[nid]; }).filter(Boolean);
                for (var i = 0; i < pts.length - 1; i++) {
                    var a = pts[i], b = pts[i + 1];
                    var midX = (a.x + b.x) / 2;
                    // Sample the bezier coarsely for hit-testing -- cheap and adequate at this scale.
                    var prevX = a.x, prevY = a.y;
                    for (var t = 0.1; t <= 1.0001; t += 0.1) {
                        var mt = 1 - t;
                        var x = mt * mt * mt * a.x + 3 * mt * mt * t * midX + 3 * mt * t * t * midX + t * t * t * b.x;
                        var yy = mt * mt * mt * a.y + 3 * mt * mt * t * a.y + 3 * mt * t * t * b.y + t * t * t * b.y;
                        var d = distToSegment(localX, localY, prevX, prevY, x, yy);
                        if (d < edgeBestDist) { edgeBestDist = d; best = { type: 'edge', id: e.id }; }
                        prevX = x; prevY = yy;
                    }
                }
            });
            return best;
        }

        function propagate() {
            affected = {};
            var disabledIds = Object.keys(disabled).filter(function (id) { return disabled[id]; });
            if (!disabledIds.length) { renderAffectedItems(); draw(); return; }
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
            if (config.panelId) {
                var panel = document.getElementById(config.panelId);
                if (panel && !panel.classList.contains('open')) {
                    panel.classList.add('open');
                    var toggleBtn = config.panelToggleId && document.getElementById(config.panelToggleId);
                    if (toggleBtn) { toggleBtn.setAttribute('aria-expanded', 'true'); toggleBtn.textContent = 'Search & detail ▴'; }
                }
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
                var metaBits = [n.type || 'node', LANE_LABEL[laneOf(n)] || laneOf(n)];
                html = '<h3>' + esc(n.name) + '</h3>' +
                    '<div class="meta">' + metaBits.map(esc).join(' · ') + '</div>' +
                    (n.vulnerability_factors && n.vulnerability_factors.length
                        ? '<p class="note">' + n.vulnerability_factors.map(function (v) { return esc(v.text); }).join(' ') + '</p>' : '') +
                    (n.type === 'item' && n.page ? '<p class="note"><a href="../' + esc(n.page).replace(/^\.\.\//, '') + '">View item page →</a></p>' : '');
            } else {
                var edgeMetaBits = [(e.edge_type || 'connection').replace(/_/g, ' ')];
                if (e.category) edgeMetaBits.push(e.category);
                html = '<h3>' + esc(e.name) + '</h3>' +
                    '<div class="meta">' + edgeMetaBits.map(esc).join(' · ') + '</div>' +
                    (e.commodities && e.commodities.length ? '<p class="note">Commodities: ' + esc(e.commodities.join(', ')) + '</p>' : '') +
                    (e.match_basis ? '<p class="note">' + esc(e.match_basis) + '</p>' : '');
            }
            el.innerHTML = '<div class="sim-node-detail">' + html +
                    '<button class="' + (isDis ? 'restore' : '') + '" id="lineage-toggle-btn">' + (isDis ? 'Restore' : 'Disable') + '</button>' +
                '</div>';
            document.getElementById('lineage-toggle-btn').addEventListener('click', function () { toggleDisabled(id); });
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
            if (!anyDisabled) { el.innerHTML = ''; if (wrapEl) wrapEl.hidden = true; return; }
            var rows = [];
            (data.item_links || []).forEach(function (link) {
                var minHop = null;
                (link.linked_edges || []).forEach(function (eid) {
                    var h = disabled[eid] ? 0 : affected[eid];
                    if (h !== undefined && (minHop === null || h < minHop)) minHop = h;
                });
                if (minHop !== null) rows.push({ link: link, hop: minHop });
            });
            data.nodes.forEach(function (n) {
                if (n.type !== 'item') return;
                var h = disabled[n.id] ? 0 : affected[n.id];
                if (h === undefined) return;
                if (rows.some(function (r) { return r.link.item_id === n.id.replace(/^item-[^-]+-/, ''); })) return;
                rows.push({ link: { item_name: n.name, item_page: n.page, category: n.category_id }, hop: h });
            });
            rows.sort(function (a, b) { return a.hop - b.hop; });
            if (wrapEl) wrapEl.hidden = false;
            if (!rows.length) { el.innerHTML = '<p class="mprow-meta">No linked items reachable from what\'s currently disabled.</p>'; return; }
            el.innerHTML = rows.slice(0, 60).map(function (r) {
                var page = r.link.item_page ? ('../' + r.link.item_page.replace(/^\.\.\//, '')) : '#';
                return '<a class="map-path-row" style="display:block; text-decoration:none;" href="' + esc(page) + '">' +
                    '<div class="mprow-name">' + esc(r.link.item_name) + '</div>' +
                    '<div class="mprow-meta">' + esc(r.link.category || '') + ' · ' + (r.hop === 0 ? 'directly disabled' : r.hop + ' hop' + (r.hop > 1 ? 's' : '') + ' away') + '</div>' +
                '</a>';
            }).join('');
        }

        function setupInteraction() {
            var tooltipEl = config.tooltipId ? document.getElementById(config.tooltipId) : null;
            zoomBehavior = d3.zoom().scaleExtent([0.02, 30]).on('zoom', function (event) {
                transform = event.transform;
                if (tooltipEl) tooltipEl.hidden = true;
                draw();
            });
            d3.select(canvas).call(zoomBehavior);

            canvas.addEventListener('click', function (event) {
                var hit = hitTest(event.clientX, event.clientY);
                if (hit) toggleDisabled(hit.id);
            });
            canvas.addEventListener('mousemove', function (event) {
                var hit = hitTest(event.clientX, event.clientY);
                var newHover = hit ? hit.id : null;
                if (newHover !== hoveredId) { hoveredId = newHover; draw(); }
                canvas.style.cursor = hit ? 'pointer' : 'grab';
                if (tooltipEl) {
                    if (hit) {
                        var rect = canvas.getBoundingClientRect();
                        var obj = hit.type === 'node' ? nodeById[hit.id] : edgeById[hit.id];
                        tooltipEl.textContent = obj ? obj.name : '';
                        tooltipEl.style.left = (event.clientX - rect.left) + 'px';
                        tooltipEl.style.top = (event.clientY - rect.top) + 'px';
                        tooltipEl.hidden = false;
                    } else tooltipEl.hidden = true;
                }
            });
            canvas.addEventListener('mouseleave', function () { if (tooltipEl) tooltipEl.hidden = true; });
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

        fetch(config.graphUrl + '?t=' + Date.now()).then(function (r) { return r.json(); }).then(function (g) {
            data = g;
            canvas = document.getElementById(config.canvasId);
            wrap = document.querySelector(config.canvasWrapSelector);
            ctx = canvas.getContext('2d');
            buildAdjacency();
            computeLayout();
            resizeCanvas();
            window.addEventListener('resize', resizeCanvas);
            setupInteraction();
            setupSearch();
            renderNodeList();
            renderAffectedItems();

            // Start the view at the top-left of the lineage (the Energy lane, backbone column)
            // rather than fitting the whole chart into view -- it should feel like scrolling
            // into a long chart, not seeing the whole thing at once.
            transform = d3.zoomIdentity.translate(40, -laneTop.energy + 40).scale(1.1);
            d3.select(canvas).call(zoomBehavior.transform, transform);

            var itemCount = data.nodes.filter(function (n) { return n.type === 'item'; }).length;
            document.getElementById(config.stampId).textContent =
                data.nodes.length + ' nodes (' + itemCount + ' items), ' + data.edges.length + ' connections' +
                (data.generated ? ' · updated ' + data.generated : '');
            document.getElementById(config.listHeadId).textContent = data.nodes.length + ' nodes';
        }).catch(function (err) {
            document.getElementById(config.stampId).textContent = 'Graph data unavailable';
            console.error('GISimulationLineage load error:', err);
        });
    }

    global.GISimulationLineage = { init: init };
})(window);
