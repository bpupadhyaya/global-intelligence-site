// GISimulation — the Global Simulation page's Canvas-based interactive graph renderer.
// Reuses GIMap's existing SVG country-outline base layer unchanged; draws the dynamic graph
// (nodes, edges, disable/propagation state) on a Canvas overlay stacked on top of it, since a
// graph with thousands of nodes/edges would make the DOM itself the bottleneck if it were SVG.
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

    function init(config) {
        var map, canvas, ctx, dpr;
        var data = null; // {nodes, edges, item_links}
        var nodeById = {}, edgesByNodeId = {}; // adjacency for propagation
        var disabled = {}; // id -> true, for both node and edge ids
        var affected = {}; // id -> hop distance (0 = disabled itself), across nodes+edges
        var transform = d3.zoomIdentity;
        var hoveredId = null, selectedId = null;
        var zoomBehavior;

        function buildAdjacency() {
            nodeById = {};
            data.nodes.forEach(function (n) { nodeById[n.id] = n; });
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

        function projectNode(n) {
            var lngLat = resolveNodeLngLat(n);
            if (!lngLat) return null;
            var p = map.project(lngLat);
            if (!p) return null;
            return { x: p[0], y: p[1] };
        }

        function resizeCanvas() {
            var wrap = document.querySelector(config.canvasWrapSelector);
            var rect = wrap.getBoundingClientRect();
            dpr = window.devicePixelRatio || 1;
            canvas.width = Math.round(rect.width * dpr);
            canvas.height = Math.round(rect.height * dpr);
            canvas.style.width = rect.width + 'px';
            canvas.style.height = rect.height + 'px';
            draw();
        }

        // Canvas coordinate space matches the SVG's 960x500 viewBox, scaled to the actual
        // rendered pixel size -- same viewBox-to-pixel mapping the SVG base map already uses.
        function scaleFactor() {
            return (canvas.width / dpr) / map.width;
        }

        function draw() {
            if (!data) return;
            ctx.save();
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, canvas.width / dpr, canvas.height / dpr);
            var k = scaleFactor();
            ctx.translate(transform.x * k / transform.k * transform.k, transform.y * k / transform.k * transform.k);
            ctx.translate(transform.x, transform.y);
            ctx.scale(transform.k * k, transform.k * k);

            // Edges first (under nodes)
            data.edges.forEach(function (e) {
                var pts = (e.nodes || []).map(function (nid) { return { id: nid, p: projectNode(nodeById[nid]) }; }).filter(function (x) { return x.p; });
                if (pts.length < 1) return;
                var isDisabled = !!disabled[e.id];
                var hop = affected[e.id];
                var color = isDisabled ? '#dc2626' : (hop !== undefined ? '#8a5a06' : '#8fa398');
                var alpha = isDisabled ? 1 : (hop !== undefined ? Math.max(0.25, 1 - hop * HOP_DECAY) : 0.35);
                ctx.strokeStyle = color;
                ctx.globalAlpha = alpha;
                ctx.lineWidth = (isDisabled ? 2.5 : hop !== undefined ? 1.8 : 1) / (transform.k * k);
                if (pts.length === 1) {
                    // Single-chokepoint edge: draw a short radial tick so it's visible without a second endpoint.
                    var p = pts[0].p;
                    ctx.beginPath();
                    ctx.arc(p.x, p.y, 6 / (transform.k * k), 0, Math.PI * 2);
                    ctx.stroke();
                } else {
                    ctx.beginPath();
                    ctx.moveTo(pts[0].p.x, pts[0].p.y);
                    for (var i = 1; i < pts.length; i++) ctx.lineTo(pts[i].p.x, pts[i].p.y);
                    ctx.stroke();
                }
                ctx.globalAlpha = 1;
            });

            // Nodes on top
            data.nodes.forEach(function (n) {
                var p = projectNode(n);
                if (!p) return;
                var isDisabled = !!disabled[n.id];
                var hop = affected[n.id];
                var r = (isDisabled ? 6 : n.id === selectedId ? 5.5 : 4) / (transform.k * k);
                var color = isDisabled ? '#dc2626' : hop !== undefined ? '#8a5a06' : '#168158';
                ctx.beginPath();
                ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
                ctx.fillStyle = color;
                ctx.globalAlpha = isDisabled ? 1 : hop !== undefined ? Math.max(0.35, 1 - hop * HOP_DECAY) : 0.85;
                ctx.fill();
                if (n.id === hoveredId || n.id === selectedId) {
                    ctx.lineWidth = 1.5 / (transform.k * k);
                    ctx.strokeStyle = '#12211b';
                    ctx.globalAlpha = 1;
                    ctx.stroke();
                }
                ctx.globalAlpha = 1;
            });
            ctx.restore();
        }

        function hitTest(clientX, clientY) {
            var rect = canvas.getBoundingClientRect();
            var k = scaleFactor();
            var localX = (clientX - rect.left - transform.x) / (transform.k * k);
            var localY = (clientY - rect.top - transform.y) / (transform.k * k);
            var best = null, bestDist = 14 / (transform.k * k);
            data.nodes.forEach(function (n) {
                var p = projectNode(n);
                if (!p) return;
                var d = Math.hypot(p.x - localX, p.y - localY);
                if (d < bestDist) { bestDist = d; best = { type: 'node', id: n.id }; }
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
        }

        function renderDetail(id) {
            var el = document.getElementById(config.detailId);
            var n = nodeById[id];
            if (!n) { el.innerHTML = ''; return; }
            selectedId = id;
            var isDis = !!disabled[id];
            el.innerHTML =
                '<div class="sim-node-detail">' +
                    '<h3>' + esc(n.name) + '</h3>' +
                    '<div class="meta">' + esc(n.type || 'node') + (n.lat !== null && n.lat !== undefined ? ' · ' + n.lat.toFixed(2) + ', ' + n.lng.toFixed(2) : n.country ? ' · ' + esc(n.country) : '') + '</div>' +
                    (n.vulnerability_factors && n.vulnerability_factors.length
                        ? '<p class="note">' + n.vulnerability_factors.map(function (v) { return esc(v.text); }).join(' ') + '</p>'
                        : '') +
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
            var anyDisabled = Object.keys(disabled).some(function (k) { return disabled[k]; });
            if (!anyDisabled) {
                el.innerHTML = '<p class="aspect-foot" style="grid-column:1/-1;">Nothing disabled yet — click a node or route on the map above.</p>';
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
            rows.sort(function (a, b) { return a.hop - b.hop; });
            if (!rows.length) {
                el.innerHTML = '<p class="aspect-foot" style="grid-column:1/-1;">No linked items are reachable from what\'s currently disabled.</p>';
                return;
            }
            el.innerHTML = rows.map(function (r) {
                return '<a class="card" href="' + esc(r.link.item_page) + '" style="display:block; text-decoration:none;">' +
                    '<h3>' + esc(r.link.item_name) + '</h3>' +
                    '<p>' + esc(r.link.category) + ' · ' + (r.hop === 0 ? 'directly disabled' : r.hop + ' hop' + (r.hop > 1 ? 's' : '') + ' away') + '</p>' +
                '</a>';
            }).join('');
        }

        function setupInteraction() {
            zoomBehavior = d3.zoom().scaleExtent([1, 12]).on('zoom', function (event) {
                transform = event.transform;
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
            map = global.GIMap.init(config.basemapSelector, atlas);
            canvas = document.getElementById(config.canvasId);
            ctx = canvas.getContext('2d');
            buildAdjacency();
            resizeCanvas();
            window.addEventListener('resize', resizeCanvas);
            setupInteraction();
            setupSearch();
            renderNodeList();
            renderAffectedItems();

            var stampEl = document.getElementById(config.stampId);
            stampEl.textContent = data.nodes.length + ' nodes, ' + data.edges.length + ' routes, ' +
                (data.item_links || []).length + ' items linked' + (data.generated ? ' · updated ' + data.generated : '');
            document.getElementById(config.captionId).textContent =
                'Click a node or route to disable it and see the effect ripple outward. Drag to pan, scroll to zoom.';
            document.getElementById(config.listHeadId).textContent = data.nodes.length + ' nodes';
            draw();
        }).catch(function (err) {
            document.getElementById(config.stampId).textContent = 'Graph data unavailable';
            document.getElementById(config.captionId).textContent = 'Could not load the simulation graph.';
            console.error('GISimulation load error:', err);
        });
    }

    global.GISimulation = { init: init };
})(window);
