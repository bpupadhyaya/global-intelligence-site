// GICommodityPage — shared page logic for every /commodities/*/ page (Gold, Silver, Copper,
// ...). Handles: fetching reserves/mines/price data, rendering the two-layer bubble map (via
// GIMap), the searchable sidebar list, and the detail cards below. Each page just supplies a
// small config object naming its data files and a few display labels -- everything else
// (radius scaling, search, layer toggle, card rendering) is identical across commodities.
//
// Expects these DOM ids to exist on the page (see commodities/gold/index.html for the
// reference markup): #world-map, #map-caption, #map-legend, #map-path-list, #map-list-head,
// #commodity-search, #layer-reserves, #layer-mines, #commodity-price-stamp, #commodity-root.
//
// Requires d3.v7, topojson-client.v3, and world-map.js to already be loaded.
(function (global) {
    function esc(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // Compact magnitude formatting ($1.23T / $456.7B / $12.3M) -- dollar figures on large
    // reserves (especially copper, reported in millions of tonnes) run past readable plain
    // numbers.
    function formatUsd(n) {
        if (n === null || n === undefined || isNaN(n)) return null; // no price data (e.g. Wheat) -- callers must handle null, never print "$NaN"
        var abs = Math.abs(n);
        if (abs >= 1e12) return '$' + (n / 1e12).toFixed(2) + 'T';
        if (abs >= 1e9) return '$' + (n / 1e9).toFixed(2) + 'B';
        if (abs >= 1e6) return '$' + (n / 1e6).toFixed(1) + 'M';
        return '$' + Math.round(n).toLocaleString('en-US');
    }
    function usdFor(tonnes, usdPerKg) { return usdPerKg != null ? formatUsd(tonnes * 1000 * usdPerKg) : null; }
    // Same compaction for tonnage -- copper reserves run into the hundreds of millions of
    // tonnes, unreadable as a plain comma-grouped number.
    function formatTonnes(t) {
        var abs = Math.abs(t);
        if (abs >= 1e9) return (t / 1e9).toFixed(2) + 'B t';
        if (abs >= 1e6) return (t / 1e6).toFixed(2) + 'M t';
        return t.toLocaleString('en-US', { maximumFractionDigits: 1 }) + ' t';
    }
    function formatKg(tonnes) {
        var kg = tonnes * 1000;
        var abs = Math.abs(kg);
        if (abs >= 1e9) return (kg / 1e9).toFixed(2) + 'B kg';
        if (abs >= 1e6) return (kg / 1e6).toFixed(2) + 'M kg';
        return Math.round(kg).toLocaleString('en-US') + ' kg';
    }

    function sourceLinks(sources) {
        if (!sources || !sources.length) return '';
        return '<h3>Sources</h3><ul>' + sources.map(function (s) {
            return '<li><a href="' + esc(s.url) + '" rel="noopener" target="_blank" ' +
                'style="color:var(--green-700);font-weight:600;">' + esc(s.title) + '</a></li>';
        }).join('') + '</ul>';
    }

    function init(config) {
        var cfg = Object.assign({
            reservesLayerLabel: 'Reserves by country',
            minesLayerLabel: 'Major mines',
            reservesSectionLabel: 'Reserves by country',
            minesSectionLabel: 'Major producing mines',
            reservesNoun: 'countries holding reserves',
            minesNoun: 'major producing mines',
            reservesListLabel: 'Top holders',
            minesListLabel: 'Top mines',
            priceUnit: 'oz', // 'oz' or 'lb' -- ignored if priceUrl is not set
            priceUrl: null, // optional -- if omitted, no $ values are computed or shown anywhere (e.g. Wheat, which has no live price feed)
            // Gold/Silver/Copper's "mines" layer is real point locations (lat/lng). Some items
            // (e.g. Wheat's "exports by country") have no point data, only a second per-country
            // aggregate -- set this true to resolve that layer via country centroid instead,
            // exactly like the reserves layer does.
            minesAreCountries: false,
            minesCardKicker: 'Mine', // e.g. 'Exports' when minesAreCountries is true
            reservesCardKicker: 'Reserves', // e.g. 'Production' for a crop like Wheat
            reserveExtraLine: function () { return ''; }
        }, config);

        var cardEls = {}; // domId -> <article>

        function renderCards(reserves, mines, usdPerKg) {
            var root = document.getElementById(cfg.rootId);

            function section(title) {
                var s = document.createElement('section');
                s.style.padding = '26px 0 6px';
                var h = document.createElement('h2');
                h.textContent = title;
                h.style.cssText = 'color:var(--green-900);font-size:22px;letter-spacing:-0.02em;margin-bottom:14px;';
                s.appendChild(h);
                var grid = document.createElement('div');
                grid.className = 'brief-grid';
                s.appendChild(grid);
                root.appendChild(s);
                return grid;
            }

            var reserveGrid = section(cfg.reservesSectionLabel + ' (' + reserves.length + ')');
            reserves.forEach(function (r) {
                var usdStr = usdFor(r.tonnes, usdPerKg);
                var domId = 'reserve-' + r.id;
                var card = document.createElement('article');
                card.className = 'briefing brief-card path-card';
                card.id = domId;
                card.innerHTML =
                    '<div class="aspect-path">#' + r.rank + ' · ' + cfg.reservesCardKicker + '</div>' +
                    '<h3>' + esc(r.country) + '</h3>' +
                    '<p class="path-route">' + formatTonnes(r.tonnes) + ' · ' + formatKg(r.tonnes) +
                        (usdStr ? ' · <strong style="color:var(--green-700)">' + usdStr + '</strong>' : '') + '</p>' +
                    cfg.reserveExtraLine(r) +
                    '<details class="context"><summary><span aria-hidden="true">💡</span> Details</summary>' +
                    '<div class="context-body"><p>As of ' + esc(r.as_of || 'unknown') + '.</p>' + sourceLinks(r.sources) + '</div></details>';
                reserveGrid.appendChild(card);
                cardEls[domId] = card;
            });

            var mineGrid = section(cfg.minesSectionLabel + ' (' + mines.length + ')');
            mines.forEach(function (m) {
                var domId = 'mine-' + m.id;
                var card = document.createElement('article');
                card.className = 'briefing brief-card path-card';
                card.id = domId;
                var usdStr = usdFor(m.annual_output_tonnes, usdPerKg);
                var kicker = cfg.minesAreCountries && m.rank ? ('#' + m.rank + ' · ' + cfg.minesCardKicker) : (cfg.minesCardKicker + ' · ' + esc(m.country));
                card.innerHTML =
                    '<div class="aspect-path">' + kicker + '</div>' +
                    '<h3>' + esc(m.name) + '</h3>' +
                    (m.operator ? '<p class="path-route">' + esc(m.operator) + '</p>' : '') +
                    '<p class="path-route">' + formatTonnes(m.annual_output_tonnes) + '/yr · ' + formatKg(m.annual_output_tonnes) + '/yr' +
                        (usdStr ? ' · <strong style="color:var(--amber-500)">' + usdStr + '</strong>/yr at current price' : '') + '</p>' +
                    '<details class="context"><summary><span aria-hidden="true">💡</span> Details</summary>' +
                    '<div class="context-body"><p>As of ' + esc(m.as_of || 'unknown') + '.</p>' + sourceLinks(m.sources) + '</div></details>';
                mineGrid.appendChild(card);
                cardEls[domId] = card;
            });
        }

        function openAndScrollToCard(domId) {
            var card = cardEls[domId];
            if (!card) return;
            var d = card.querySelector('details.context');
            if (d) d.open = true;
            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
            card.style.outline = '2px solid var(--green-600)';
            card.style.outlineOffset = '3px';
            setTimeout(function () { card.style.outline = ''; }, 1600);
        }

        function initMap(atlasTopo, reserves, mines, usdPerKg) {
            var map = GIMap.init('#world-map', atlasTopo);
            var bubblesGroup = map.svg.append('g').attr('id', 'geo-bubbles');

            // Perceptual (area-proportional, via sqrt) radius scale -- 2x the tonnes should
            // look ~2x the AREA, not 2x the radius.
            var MIN_R = 3, MAX_R = 26;
            function radiusScale(value, maxValue) {
                if (!maxValue) return MIN_R;
                return MIN_R + (MAX_R - MIN_R) * Math.sqrt(Math.max(value, 0) / maxValue);
            }

            var maxReserve = reserves.reduce(function (m, r) { return Math.max(m, r.tonnes); }, 0);
            var maxMine = mines.reduce(function (m, r) { return Math.max(m, r.annual_output_tonnes); }, 0);

            var items = {}; // domId -> {domId, label, meta, layer, value, bubble, searchText}

            reserves.forEach(function (r) {
                var ll = map.countryLngLat(r.country);
                if (!ll) return; // country name didn't resolve against the atlas -- skip rather than mis-plot
                var xy = map.project(ll);
                if (!xy) return;
                var domId = 'reserve-' + r.id;
                var usdStr = usdFor(r.tonnes, usdPerKg);
                var b = bubblesGroup.append('circle')
                    .attr('class', 'geo-bubble reserves')
                    .attr('cx', xy[0]).attr('cy', xy[1])
                    .attr('r', radiusScale(r.tonnes, maxReserve))
                    .attr('data-layer', 'reserves');
                var titleEl = document.createElementNS('http://www.w3.org/2000/svg', 'title');
                titleEl.textContent = r.country + ' — ' + formatTonnes(r.tonnes);
                b.node().appendChild(titleEl);
                items[domId] = { domId: domId, label: r.country, meta: '#' + r.rank + ' · ' + formatTonnes(r.tonnes) + (usdStr ? ' · ' + usdStr : ''), layer: 'reserves', value: r.tonnes, bubble: b, searchText: r.country.toLowerCase() };
            });

            mines.forEach(function (m) {
                var xy;
                if (cfg.minesAreCountries) {
                    var ll = map.countryLngLat(m.country);
                    xy = ll ? map.project(ll) : null;
                } else {
                    xy = map.project([m.lng, m.lat]);
                }
                if (!xy) return; // country/point didn't resolve -- skip rather than mis-plot
                var domId = 'mine-' + m.id;
                var usdStr = usdFor(m.annual_output_tonnes, usdPerKg);
                var b = bubblesGroup.append('circle')
                    .attr('class', 'geo-bubble mines')
                    .attr('cx', xy[0]).attr('cy', xy[1])
                    .attr('r', radiusScale(m.annual_output_tonnes, maxMine))
                    .attr('data-layer', 'mines');
                var titleEl = document.createElementNS('http://www.w3.org/2000/svg', 'title');
                titleEl.textContent = (cfg.minesAreCountries ? m.country : (m.name + ' (' + m.country + ')')) + ' — ' + formatTonnes(m.annual_output_tonnes) + '/yr';
                b.node().appendChild(titleEl);
                var metaPrefix = cfg.minesAreCountries && m.rank ? '#' + m.rank + ' · ' : m.country + ' · ';
                items[domId] = { domId: domId, label: m.name, meta: metaPrefix + formatTonnes(m.annual_output_tonnes) + '/yr' + (usdStr ? ' · ' + usdStr + '/yr' : ''), layer: 'mines', value: m.annual_output_tonnes, bubble: b, searchText: (m.name + ' ' + m.country + ' ' + (m.operator || '')).toLowerCase() };
            });

            Object.keys(items).forEach(function (id) {
                items[id].bubble
                    .on('mouseenter', function () { hoverItem(id); })
                    .on('mouseleave', function () { hoverItem(null); })
                    .on('click', function () { openAndScrollToCard(id); });
            });

            // ---------- Layer / filter state ----------
            var activeLayer = 'reserves';
            var searchQuery = '';
            var hoveredId = null;

            function currentLayerItems() {
                return Object.keys(items).filter(function (id) { return items[id].layer === activeLayer; });
            }
            function matchesSearch(id) {
                if (!searchQuery) return true;
                return items[id].searchText.indexOf(searchQuery) !== -1;
            }
            function applyLayerVisibility() {
                Object.keys(items).forEach(function (id) {
                    items[id].bubble.style('display', items[id].layer === activeLayer ? null : 'none');
                });
            }
            function applyHighlight() {
                var ids = currentLayerItems();
                var anyFilter = !!searchQuery;
                ids.forEach(function (id) {
                    var b = items[id].bubble;
                    if (hoveredId) {
                        b.classed('mm-active', id === hoveredId);
                        b.classed('mm-dim', id !== hoveredId);
                    } else if (anyFilter) {
                        var match = matchesSearch(id);
                        b.classed('mm-active', match);
                        b.classed('mm-dim', !match);
                    } else {
                        b.classed('mm-active', false);
                        b.classed('mm-dim', false);
                    }
                });
                var cap = document.getElementById('map-caption');
                if (hoveredId) {
                    cap.textContent = items[hoveredId].label + ' — ' + items[hoveredId].meta;
                } else if (anyFilter) {
                    var n = ids.filter(matchesSearch).length;
                    cap.textContent = n + ' of ' + ids.length + ' ' + (activeLayer === 'reserves' ? 'entries' : 'mines') + ' match this search.';
                } else {
                    cap.textContent = 'Bubble size ∝ tonnes · showing ' + ids.length + ' ' + (activeLayer === 'reserves' ? cfg.reservesNoun : cfg.minesNoun) + '.';
                }
                document.querySelectorAll('.map-path-row').forEach(function (row) {
                    row.classList.toggle('hovered', row.dataset.itemId === hoveredId);
                });
            }
            function hoverItem(id) { hoveredId = id; applyHighlight(); }

            function rebuildList() {
                var listEl = document.getElementById('map-path-list');
                var headEl = document.getElementById('map-list-head');
                var ids = currentLayerItems().filter(matchesSearch);
                ids.sort(function (a, b) { return items[b].value - items[a].value; });
                headEl.textContent = (activeLayer === 'reserves' ? cfg.reservesListLabel : cfg.minesListLabel) +
                    (searchQuery ? ' (' + ids.length + ' matching)' : '');
                listEl.innerHTML = '';
                if (!ids.length) {
                    var empty = document.createElement('p');
                    empty.className = 'map-empty';
                    empty.textContent = 'No matches. Try a different search.';
                    listEl.appendChild(empty);
                    return;
                }
                ids.forEach(function (id) {
                    var it = items[id];
                    var row = document.createElement('button');
                    row.type = 'button';
                    row.className = 'map-path-row';
                    row.dataset.itemId = id;
                    row.innerHTML = '<div class="mprow-name">' + esc(it.label) + '</div><div class="mprow-meta">' + esc(it.meta) + '</div>';
                    row.addEventListener('mouseenter', function () { hoverItem(id); });
                    row.addEventListener('mouseleave', function () { hoverItem(null); });
                    row.addEventListener('click', function () { openAndScrollToCard(id); });
                    listEl.appendChild(row);
                });
            }

            function renderLegend() {
                var el = document.getElementById('map-legend');
                var maxValue = activeLayer === 'reserves' ? maxReserve : maxMine;
                var cls = activeLayer === 'reserves' ? 'reserves' : 'mines';
                var steps = [0.15, 0.5, 1.0];
                var box = MAX_R * 2 + 6, mid = box / 2;
                el.innerHTML = steps.map(function (frac) {
                    var r = radiusScale(maxValue * frac, maxValue);
                    return '<span class="map-legend-swatch"><svg width="' + box + '" height="' + box + '" style="flex-shrink:0"><circle cx="' + mid + '" cy="' + mid + '" r="' + r + '" class="geo-bubble ' + cls + '"></circle></svg>' +
                        '<span style="white-space:nowrap">~' + formatTonnes(Math.round(maxValue * frac)) + '</span></span>';
                }).join('');
            }

            function refresh() { applyLayerVisibility(); rebuildList(); applyHighlight(); renderLegend(); }

            document.getElementById('layer-reserves').addEventListener('click', function () {
                activeLayer = 'reserves'; hoveredId = null;
                this.classList.add('active'); document.getElementById('layer-mines').classList.remove('active');
                refresh();
            });
            document.getElementById('layer-mines').addEventListener('click', function () {
                activeLayer = 'mines'; hoveredId = null;
                this.classList.add('active'); document.getElementById('layer-reserves').classList.remove('active');
                refresh();
            });
            document.getElementById('commodity-search').addEventListener('input', function (e) {
                searchQuery = e.target.value.trim().toLowerCase();
                rebuildList(); applyHighlight();
            });

            refresh();
        }

        function formatPriceStamp(priceData) {
            var perUnit = cfg.priceUnit === 'lb' ? priceData.usd_per_lb : priceData.usd_per_troy_oz;
            var unitLabel = cfg.priceUnit === 'lb' ? '/lb' : '/oz';
            return cfg.commodityName + ': $' + perUnit.toLocaleString('en-US') + unitLabel + ' ($' +
                priceData.usd_per_kg.toLocaleString('en-US') + '/kg) · updated ' + priceData.generated;
        }

        Promise.all([
            fetch(cfg.reservesUrl + '?t=' + Date.now()).then(function (r) { return r.json(); }),
            fetch(cfg.minesUrl + '?t=' + Date.now()).then(function (r) { return r.json(); }),
            cfg.priceUrl ? fetch(cfg.priceUrl + '?t=' + Date.now()).then(function (r) { return r.json(); }) : Promise.resolve(null),
            fetch(cfg.atlasUrl).then(function (r) { return r.json(); })
        ]).then(function (results) {
            var reservesData = results[0], minesData = results[1], priceData = results[2], atlasTopo = results[3];
            var reserves = reservesData.countries, mines = minesData.mines, usdPerKg = priceData ? priceData.usd_per_kg : null;

            var stampEl = document.getElementById('commodity-price-stamp');
            if (stampEl) stampEl.textContent = priceData ? formatPriceStamp(priceData) : cfg.noPriceStamp || (cfg.commodityName + ' — ' + reserves.length + ' countries tracked');

            renderCards(reserves, mines, usdPerKg);
            initMap(atlasTopo, reserves, mines, usdPerKg);
        }).catch(function (err) {
            document.getElementById(cfg.rootId).innerHTML =
                '<p style="color:var(--ink-faint)">Could not load the ' + cfg.commodityName.toLowerCase() + ' data — try again shortly.</p>';
            document.getElementById('map-caption').textContent = 'Could not load the map.';
            var stampEl = document.getElementById('commodity-price-stamp');
            if (stampEl) stampEl.textContent = cfg.commodityName + ' data unavailable';
            console.error(err);
        });
    }

    global.GICommodityPage = { init: init };
})(window);
