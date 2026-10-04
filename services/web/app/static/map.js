// Leaflet map of the parcel and nearby council layers, drawn from /api/map.
import {$, esc, layerName} from "./report.js";

const MAP_STYLE = {
  flood_plain: {color:"#1f6fd1", fillOpacity:.35, weight:1}, flood_prone_area: {color:"#7b3fc4", fillOpacity:.3, weight:1, dashArray:"4 3"},
  overland_flow_path: {color:"#0b3d91", weight:2, dashArray:"6 4", fillOpacity:0}, coastal_inundation: {color:"#0e8a7d", fillOpacity:.35, weight:1},
  tsunami_evacuation_zone: {color:"#d9730d", fillOpacity:.25, weight:1}, coastal_erosion: {color:"#7a4a12", fillOpacity:.3, weight:1, dashArray:"1 4"}, parcel: {color:"#111", weight:3, fillOpacity:0}};
// region-wide layers: coloured by class (light = lower), switched off at first
const LANDSLIDE = {"Very Low":"#ffffb2", "Low":"#fecc5c", "Moderate":"#fd8d3c", "High":"#f03b20", "Very High":"#bd0026"};
const CLASS_COLOURS = {landslide_shallow: LANDSLIDE, landslide_large_scale: LANDSLIDE,
  liquefaction: {"Very Low":"#ffffb2", "Unlikely":"#fecc5c", "Possible":"#f03b20"}};
const NOT_NEEDED = " The results below do not depend on the map.";
const CREDITS = " · Hazard layers: Auckland Council · Parcel: LINZ (CC BY 4.0)";
const config = fetch("config.json").then(r => r.json()).catch(() => ({}));
const legend = (title, colours) => title + ": " + Object.entries(colours).map(([name, colour]) =>
  `<span class="swatch" style="background:${colour}"></span>${name}`).join(" &nbsp; ");
let map;

export async function drawMap(request, isCurrent) {
  if (map) { map.remove(); map = null; }
  $("mapnote").textContent = "Loading map…";
  $("maplegend").innerHTML = legend("Landslide classes", LANDSLIDE) + "<br>" + legend("Liquefaction classes", CLASS_COLOURS.liquefaction);
  if (!window.L) { $("mapnote").textContent = "The map library could not be loaded." + NOT_NEEDED; return; }
  try {
    const [g, {linz_basemaps_key: key}] = await Promise.all([request, config]);
    const [w, s, e, n] = g.bbox, features = layer => g.features.filter(f => f.properties.layer === layer);
    if (!isCurrent()) return;
    map = L.map("map").fitBounds([[s, w], [n, e]]);
    const streets = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' + CREDITS});
    const aerial = key && L.tileLayer(`https://basemaps.linz.govt.nz/v1/tiles/aerial/3857/{z}/{x}/{y}.webp?api=${encodeURIComponent(key)}`, {maxZoom: 21,
      attribution: '&copy; <a href="https://www.linz.govt.nz/linz-copyright">LINZ CC BY 4.0</a> &copy; <a href="https://www.linz.govt.nz/data/linz-data/linz-basemaps/data-attribution">Imagery Basemap contributors</a>' + CREDITS});
    (aerial || streets).addTo(map);
    // an expired or refused basemap key must not leave the map blank
    if (aerial) aerial.once("tileerror", () => { map.removeLayer(aerial); streets.addTo(map); });
    // outline the queried window: layers are cut at its edge
    L.rectangle([[s, w], [n, e]], {color: "#555", weight: 1, dashArray: "2 5", fill: false, interactive: false}).addTo(map);
    const overlays = {}, nearby = [];
    for (const [layer, style] of Object.entries(MAP_STYLE)) {
      const feats = features(layer), name = layer === "parcel" ? "Parcel boundary" : layerName(layer);
      if (!feats.length) continue;
      if (layer !== "parcel") nearby.push(name);
      overlays[`<span class="swatch" style="background:${style.color}"></span>${esc(name)}`] = L.geoJSON(feats, {style}).addTo(map);
    }
    for (const [layer, colours] of Object.entries(CLASS_COLOURS)) {
      const feats = features(layer);
      if (!feats.length) continue;
      overlays[esc(layerName(layer)) + " (by class)"] = L.geoJSON(feats, {
        style: f => ({stroke: false, fillColor: colours[f.properties.class] || "#888", fillOpacity: .55}),
        onEachFeature: (f, l) => l.bindPopup(esc(layerName(layer)) + ": " + esc(f.properties.class || "not classed"))});
    }
    const [pt] = features("address");
    if (pt) L.circleMarker([pt.geometry.coordinates[1], pt.geometry.coordinates[0]], {radius: 6, color: "#111", fillColor: "#fff", fillOpacity: 1}).addTo(map);
    L.control.layers(aerial && {"Aerial photo (LINZ)": aerial, "Street map (OpenStreetMap)": streets}, overlays, {collapsed: matchMedia("(max-width: 600px)").matches}).addTo(map);  // the open list would cover a phone-width map
    // "none mapped" is only said when every layer loaded
    $("mapnote").textContent = (nearby.length ? "Shown within about 120 m: " + nearby.join(", ") + "."
        : g.unavailable.length ? "" : "No flood, coastal or tsunami layer is mapped within about 120 m.")
      + (g.unavailable.length ? " Could not load: " + g.unavailable.map(layerName).join(", ") + "." : "")
      + " The dotted box is the area checked; layers are cut off at its edge. Landslide and liquefaction can be switched on in the layer list (select an area for its class).";
  } catch (e) { if (isCurrent()) $("mapnote").textContent = "The map could not be loaded (" + e.message + ")." + NOT_NEEDED; }
}
