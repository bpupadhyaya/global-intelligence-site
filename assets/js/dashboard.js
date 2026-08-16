// GIDashboard — the Global Dashboard's zoomable-treemap page logic. One level of the
// taxonomy is laid out at a time (clicking a cell with children zooms in; a leaf either
// navigates to its existing page or, if it doesn't have one yet, shows its detail inline --
// the dashboard "refining itself" fallback for content-only leaves per the approved plan).
// Requires d3.v7 (with d3-hierarchy) already loaded on the page.
(function (global) {
    var WIDTH = 900, HEIGHT = 460;

    function esc(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function formatUsd(n) {
        if (n === null || n === undefined) return null;
        var abs = Math.abs(n);
        if (abs >= 1e12) return '$' + (n / 1e12).toFixed(2) + 'T';
        if (abs >= 1e9) return '$' + (n / 1e9).toFixed(2) + 'B';
        if (abs >= 1e6) return '$' + (n / 1e6).toFixed(1) + 'M';
        return '$' + Math.round(n).toLocaleString('en-US');
    }

    // A group of siblings being laid out needs every member to have a positive rendering
    // weight, even when its real $ value is null ("unavailable") -- an invisible zero-size
    // cell would make an item look like it doesn't exist, which is worse than an honest sliver
    // labeled "value not available". A flat constant floor (e.g. 1) would round to sub-pixel
    // and vanish next to trillion-dollar siblings, so the floor is computed relative to THIS
    // group's own real total: unvalued siblings each get ~10% of the group's valued total (or
    // an even split if literally nothing in the group has a value yet).
    function weighChildren(children) {
        var sum = children.reduce(function (a, c) { return a + ((c.valuation && c.valuation.usd) || 0); }, 0);
        var floor = sum > 0 ? sum * 0.10 : 1;
        return children.map(function (c) {
            var v = (c.valuation && c.valuation.usd) || 0;
            return { ref: c, value: v > 0 ? v : floor };
        });
    }

    function init(config) {
        var data = null; // full taxonomy.json
        var path = []; // array of nodes from root to the currently zoomed node (inclusive)
        var selectedLeaf = null; // a leaf without its own page, shown inline in the detail panel

        var svg = d3.select(config.svgSelector).attr('viewBox', '0 0 ' + WIDTH + ' ' + HEIGHT);
        var cellsGroup = svg.append('g');

        function currentNode() { return path[path.length - 1]; }

        function renderBreadcrumb() {
            var el = document.getElementById(config.breadcrumbId);
            el.innerHTML = '';
            path.forEach(function (node, i) {
                if (i > 0) {
                    var sep = document.createElement('span');
                    sep.className = 'sep';
                    sep.textContent = '/';
                    el.appendChild(sep);
                }
                var btn = document.createElement('button');
                btn.type = 'button';
                btn.textContent = node.name;
                if (i === path.length - 1) btn.setAttribute('aria-current', 'true');
                btn.addEventListener('click', function () {
                    path = path.slice(0, i + 1);
                    selectedLeaf = null;
                    renderAll();
                });
                el.appendChild(btn);
            });
        }

        function categoryClass(node) {
            return node.color || 'default';
        }

        function renderDetail(node, isZoomLevel) {
            var el = document.getElementById(config.detailId);
            var v = node.valuation || {};
            var valHtml = v.usd !== null && v.usd !== undefined
                ? '<div class="val">' + formatUsd(v.usd) + (v.partial ? '<span class="dash-partial-flag">PARTIAL</span>' : '') + '</div>'
                : '<div class="val unavailable">Value not yet available</div>';
            var metaHtml = v.as_of ? '<div class="meta">' + (v.method === 'aggregated_children' ? 'Aggregated as of' : 'As of') + ' ' + esc(v.as_of) + '</div>' : '';
            var noteHtml = v.note ? '<p class="note">' + esc(v.note) + '</p>' : '';
            var srcHtml = (v.sources && v.sources.length)
                ? '<p class="note">' + v.sources.map(function (s) { return '<a href="' + esc(s.url) + '" rel="noopener" target="_blank" style="color:var(--green-700);font-weight:600;">' + esc(s.title) + '</a>'; }).join(' · ') + '</p>'
                : '';
            var linkHtml = node.page ? '<a class="btn" href="' + esc(node.page) + '">Open ' + esc(node.name) + ' →</a>' : '';
            var kicker = isZoomLevel ? (node.children ? node.children.length + ' items inside' : '') : '';

            el.innerHTML = '<div class="dash-node-detail">' +
                '<h3>' + esc(node.name) + '</h3>' +
                (kicker ? '<div class="meta">' + kicker + '</div>' : '') +
                valHtml + metaHtml + noteHtml + srcHtml + linkHtml +
                '</div>';
        }

        function renderList(node) {
            var listEl = document.getElementById(config.listId);
            var headEl = document.getElementById(config.listHeadId);
            headEl.textContent = node.children ? node.children.length + ' item' + (node.children.length === 1 ? '' : 's') + ' in ' + node.name : node.name;
            listEl.innerHTML = '';
            (node.children || []).forEach(function (child) {
                var row = document.createElement('button');
                row.type = 'button';
                row.className = 'map-path-row';
                var v = child.valuation || {};
                var valText = v.usd !== null && v.usd !== undefined ? formatUsd(v.usd) + (v.partial ? ' (partial)' : '') : 'value not available';
                row.innerHTML = '<div class="mprow-name">' + esc(child.name) + '</div><div class="mprow-meta">' + esc(valText) + '</div>';
                row.addEventListener('mouseenter', function () { renderDetail(child, false); });
                row.addEventListener('click', function () { activate(child); });
                listEl.appendChild(row);
            });
        }

        function activate(node) {
            if (node.children && node.children.length) {
                path.push(node);
                selectedLeaf = null;
                renderAll();
            } else if (node.page) {
                window.location.href = node.page;
            } else {
                selectedLeaf = node;
                renderDetail(node, false);
            }
        }

        function renderTreemap() {
            var node = currentNode();
            var kids = weighChildren(node.children || []);
            var hierarchyData = { children: kids };
            var root = d3.hierarchy(hierarchyData).sum(function (d) { return d.value; });
            d3.treemap().size([WIDTH, HEIGHT]).paddingInner(3).round(true)(root);

            // Color represents "which top-level category" a node belongs to. If the node
            // currently zoomed into already has a category color, every descendant inherits it
            // (e.g. inside Commodities, Gold/Silver/Copper all render blue); otherwise (at the
            // root, which has no category of its own) each child's OWN color is used (Commodities
            // blue, Trade green, ...) -- this is what actually assigns the categorical colors in
            // the first place.
            var colorSource = node.color ? node : null;

            cellsGroup.selectAll('*').remove();
            var cell = cellsGroup.selectAll('g.cell')
                .data(root.leaves())
                .join('g')
                .attr('class', 'cell')
                .attr('transform', function (d) { return 'translate(' + d.x0 + ',' + d.y0 + ')'; });

            cell.append('rect')
                .attr('class', function (d) {
                    var n = d.data.ref;
                    var unavailable = !n.valuation || n.valuation.usd === null || n.valuation.usd === undefined;
                    return 'dash-cell ' + (unavailable ? 'unavailable' : categoryClass(colorSource || n));
                })
                .attr('width', function (d) { return Math.max(0, d.x1 - d.x0); })
                .attr('height', function (d) { return Math.max(0, d.y1 - d.y0); })
                .attr('tabindex', 0)
                .attr('role', 'button')
                .on('mouseenter', function (event, d) { renderDetail(d.data.ref, false); })
                .on('click', function (event, d) { activate(d.data.ref); })
                .on('keydown', function (event, d) { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(d.data.ref); } });

            cell.each(function (d) {
                var w = d.x1 - d.x0, h = d.y1 - d.y0;
                if (w < 34 || h < 22) return; // too small to label without overflow
                var n = d.data.ref;
                var unavailable = !n.valuation || n.valuation.usd === null || n.valuation.usd === undefined;
                var g = d3.select(this);
                g.append('text')
                    .attr('class', 'dash-cell-label' + (unavailable ? ' dark' : ''))
                    .attr('x', 8).attr('y', 8)
                    .text(n.name.length > Math.floor(w / 7) ? n.name.slice(0, Math.max(3, Math.floor(w / 7) - 1)) + '…' : n.name);
                if (h > 40) {
                    var valText = unavailable ? 'n/a' : formatUsd(n.valuation.usd);
                    g.append('text')
                        .attr('class', 'dash-cell-value' + (unavailable ? ' dark' : ''))
                        .attr('x', 8).attr('y', 25)
                        .text(valText);
                }
            });
        }

        function renderStats() {
            var gdpEl = document.getElementById(config.gdpStatId);
            var trackedEl = document.getElementById(config.trackedStatId);
            if (data.world_gdp && data.world_gdp.usd !== null) {
                gdpEl.querySelector('.num').textContent = formatUsd(data.world_gdp.usd);
                gdpEl.querySelector('.stat-caption').textContent = 'World GDP, as of ' + data.world_gdp.as_of;
            } else {
                gdpEl.querySelector('.num').textContent = '—';
                gdpEl.querySelector('.stat-caption').textContent = 'World GDP figure not yet added';
            }
            var t = data.tracked_total;
            trackedEl.querySelector('.num').textContent = t.usd !== null ? formatUsd(t.usd) : '—';
            trackedEl.querySelector('.stat-caption').textContent =
                data.coverage.leaf_nodes_with_valuation + ' of ' + (data.coverage.total_nodes - countBranches(data.root)) + ' items valued' +
                (t.partial ? ' · partial coverage' : '');
        }

        function countBranches(node) {
            var b = node.children ? 1 : 0;
            (node.children || []).forEach(function (c) { b += countBranches(c); });
            return b;
        }

        function renderTable() {
            var tbody = document.getElementById(config.tableBodyId);
            tbody.innerHTML = '';
            function walk(node, depth) {
                var tr = document.createElement('tr');
                tr.dataset.depth = depth;
                var v = node.valuation || {};
                var valText = v.usd !== null && v.usd !== undefined ? formatUsd(v.usd) + (v.partial ? ' (partial)' : '') : 'not available';
                tr.innerHTML = '<td class="node-name">' + esc(node.name) + '</td>' +
                    '<td class="val' + (v.usd === null || v.usd === undefined ? ' unavailable' : '') + '">' + esc(valText) + '</td>' +
                    '<td>' + esc(v.method || '') + '</td>';
                tbody.appendChild(tr);
                (node.children || []).forEach(function (c) { walk(c, depth + 1); });
            }
            walk(data.root, 0);
        }

        function renderAll() {
            renderBreadcrumb();
            renderTreemap();
            renderList(currentNode());
            renderDetail(currentNode(), true);
            var cap = document.getElementById(config.captionId);
            cap.textContent = 'Cell size ∝ $ value · click a category to zoom in, click an item to open it.';
        }

        fetch(config.taxonomyUrl + '?t=' + Date.now()).then(function (r) { return r.json(); }).then(function (json) {
            data = json;
            path = [data.root];
            renderStats();
            renderTable();
            renderAll();
        }).catch(function (err) {
            document.getElementById(config.captionId).textContent = 'Could not load the dashboard data — try again shortly.';
            console.error(err);
        });
    }

    global.GIDashboard = { init: init };
})(window);
