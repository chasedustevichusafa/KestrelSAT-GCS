# Graph appearance and RSSI overlay — 2026-11-03

## White or black graphs

Graphs now default to **white**, regardless of the main interface theme. Select
**Options → Graph Background → White / Black**, or use **Preferences → Appearance
→ Graph background**. The selection is saved and applies immediately to open
telemetry, Transfer Stats and Link Tracker graphs, newly opened graph windows,
and PNG exports. Axes, titles, legends and automatic line colours follow the
selected background. User-picked telemetry channel colours remain unchanged.
The main interface theme is a separate preference.

## Overlay RSSI on transfer speed

In **Transfer Stats**, enable **Overlay RSSI on speed graph (right dBm axis)**.
The download/upload curves use the left rate axis; the dashed RSSI curve uses a
separate right axis in dBm. Both share the original elapsed-time coordinates.
Each axis scales independently. RSSI is not normalized, converted into speed or
filled with zeros when missing. The checkbox changes presentation only; the
separate **Record ground receiver RSSI** checkbox controls measurement during
transfers. The dedicated Link Tracker continues to have its own graphs.

The overlay replaces the separate RSSI panel while enabled. Disable it to return
to stacked rate/RSSI panels. The preference is saved across graph reopen and
application restart. Zoom/pan still controls the shared time axis; resizing keeps
the two views aligned. The legend identifies RSSI as using the right axis.

## Exporting

- **Combined** exports the visible layout: either stacked panels or the speed
  graph with RSSI overlaid, including its two axes.
- **Rates** includes the RSSI overlay when it is enabled.
- **RSSI** always exports a standalone RSSI graph. The app temporarily restores
  that panel for rendering, then returns to the selected overlay layout.
- White/black appearance is included in exported images, not just the live view.
- Existing **Full timeline / Current view** choices still apply. After export,
  the previous data view, zoom, axis settings and overlay preference are restored,
  including when export fails.

Raw measurements, polling rates, transfer behavior, CSV contents and firmware
are unchanged. This build includes the Link Tracker feature from its existing
branch; the formatting preference also applies to all four Link Tracker plots.
