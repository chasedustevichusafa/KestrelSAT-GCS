"""Graph-only palettes, independent of the desktop controls' selected theme.

Copy tokens instead of changing themes.CURRENT: the monitor, buttons and custom
channel colours must keep their own appearance. All graph windows and exporters
use the same selected palette, including windows created after the preference.
"""
from . import themes

CHOICES = ('white', 'black')


def colors(app, base=None):
    background = getattr(app, 'graph_background', 'white')
    style = themes.THEMES['dark' if background == 'black' else 'light']
    result = dict(themes.CURRENT if base is None else base)
    result.update({key: value for key, value in style.items() if key.startswith('plot_')})
    result['error'] = style['error']
    if background == 'black':
        result.update(plot_bg='#000000', plot_fg='#FFFFFF', plot_axis='#B0B0B0',
                      plot_legend_bg='#000000E8', plot_legend_border='#808080')
    return result


def theme_plot(plot, palette):
    """Refresh existing axes, labels, titles and legends (not just new items)."""
    import pyqtgraph as pg
    for name in ('left', 'bottom', 'right', 'top'):
        axis = plot.getAxis(name)
        axis.setPen(palette['plot_axis'])
        axis.setTextPen(palette['plot_fg'])
        axis.setLabel(text=axis.labelText, units=axis.labelUnits,
                      unitPrefix=axis.labelUnitPrefix,
                      **dict(axis.labelStyle, color=palette['plot_fg']))
    plot.showGrid(x=True, y=True, alpha=palette['plot_grid_alpha'])
    plot.titleLabel.setText(plot.titleLabel.text, color=palette['plot_fg'])
    if plot.legend is not None:
        plot.legend.setLabelTextColor(palette['plot_fg'])
        plot.legend.setBrush(pg.mkBrush(palette['plot_legend_bg']))
        plot.legend.setPen(pg.mkPen(palette['plot_legend_border']))
        # Existing LabelItems retain their previously rendered HTML colour.
        for _, label in plot.legend.items:
            label.setText(label.text)
