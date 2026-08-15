// GIMap — shared D3 world-map bootstrap used by /supply-chain/ and every /commodities/*/
// page. Renders the base country layer into an existing <svg viewBox="0 0 960 500"> and
// hands back helpers for projecting a [lng,lat] point and looking up a country's centroid
// by its exact world-atlas name (data/world-atlas-countries-110m.json's properties.name).
// Requires d3.v7 and topojson-client.v3 to already be loaded on the page.
(function (global) {
    var WIDTH = 960, HEIGHT = 500;

    // d3.geoCentroid() on a country's actual polygon lands somewhere misleading for a
    // handful of countries (mid-ocean between islands, or skewed toward a huge sparsely-
    // populated region) — override those with a single hand-picked representative point.
    var CENTROID_OVERRIDES = {
        'United States of America': [39.8, -98.5],
        'Russia': [55.75, 37.6],
        'France': [46.6, 2.2],
        'Indonesia': [-6.2, 106.8],
        'Norway': [61.0, 9.0],
        'Philippines': [14.6, 121.0],
        'New Zealand': [-41.3, 174.8]
    };

    function init(svgSelector, atlasTopo) {
        var svg = d3.select(svgSelector);
        var countries = topojson.feature(atlasTopo, atlasTopo.objects.countries);

        var projection = d3.geoNaturalEarth1().fitSize([WIDTH, HEIGHT], countries);
        var geoPathGen = d3.geoPath(projection);

        svg.append('g').selectAll('path.geo-country')
            .data(countries.features)
            .join('path')
            .attr('class', 'geo-country')
            .attr('d', geoPathGen);

        var centroidByName = {};
        countries.features.forEach(function (f) {
            var name = f.properties.name;
            centroidByName[name] = CENTROID_OVERRIDES[name] ?
                [CENTROID_OVERRIDES[name][1], CENTROID_OVERRIDES[name][0]] : // to [lng,lat]
                d3.geoCentroid(f);
        });

        return {
            svg: svg,
            width: WIDTH,
            height: HEIGHT,
            countryNames: countries.features.map(function (f) { return f.properties.name; }),
            // Exact world-atlas name -> [lng,lat] centroid, or null if the name doesn't match.
            countryLngLat: function (name) { return centroidByName[name] || null; },
            // [lng,lat] -> projected [x,y] on the 960x500 viewBox, or null if unprojectable.
            project: function (lngLat) { return projection(lngLat); }
        };
    }

    global.GIMap = { init: init };
})(window);
