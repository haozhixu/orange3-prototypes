import sys
from enum import IntEnum
from itertools import chain
from types import SimpleNamespace as namespace
from collections import namedtuple
from xml.sax.saxutils import escape

import numpy as np

from AnyQt.QtCore import Qt, QSize, QLineF
from AnyQt.QtGui import QPainter, QPen, QColor
from AnyQt.QtWidgets import QApplication, QSizePolicy, QGraphicsLineItem

import pyqtgraph as pg
from pyqtgraph.graphicsItems.ViewBox import ViewBox
from pyqtgraph.functions import mkPen

from Orange.data import Table, DiscreteVariable
from Orange.widgets import gui, settings
from Orange.widgets.utils.annotated_data import (create_annotated_table,
                                                 ANNOTATED_DATA_SIGNAL_NAME)
from Orange.widgets.utils.colorpalette import ColorPaletteGenerator
from Orange.widgets.utils.itemmodels import DomainModel
from Orange.widgets.utils.plot import OWPlotGUI, SELECT, PANNING, ZOOMING
from Orange.widgets.visualize.owdistributions import (
    LegendItem, ScatterPlotItem
)
from Orange.widgets.widget import OWWidget, Input, Output, Msg


def ccw(a, b, c):
    """
    Checks whether three points are listed in a counterclockwise order.
    """
    return (c.y - a.y) * (b.x - a.x) > (b.y - a.y) * (c.x - a.x)


def intersects(a, b, c, d):
    """
    Checks whether line segment a (given points a and b) intersects with line
    segment b (given points c and d).
    """
    return ccw(a, c, d) != ccw(b, c, d) and ccw(a, b, c) != ccw(a, b, d)


def line_segments_line_intersect(p1, p2, item):
    """
    Checks if any line segments (item.data) intersect line (p1, p2).
    """
    point = namedtuple("point", ["x", "y"])
    for i in range(len(item.xData) - 1):
        a = point(item.xData[i], item.yData[i])
        b = point(item.xData[i + 1], item.yData[i + 1])
        if intersects(point(p1.x(), p1.y()), point(p2.x(), p2.y()), a, b):
            return True
    return False


class LinePlotStyle:
    DEFAULT_COLOR = QColor(Qt.darkGray)
    SELECTION_LINE_COLOR = QColor(Qt.black)
    SELECTION_LINE_WIDTH = 2

    MEAN_WIDTH = 8

    UNSELECTED_LINE_WIDTH = 1
    UNSELECTED_LINE_ALPHA = 100
    UNSELECTED_LINE_ALPHA_WITH_SELECTION = 50

    SELECTED_LINE_WIDTH = 4
    SELECTED_LINE_ALPHA = 170

    RANGE_ALPHA = 25

    def __call__(self, n):
        return ColorPaletteGenerator(n)


class LinePlotItem(pg.PlotCurveItem):
    def __init__(self, master, index, instance_id, x, y, color):
        color.setAlpha(LinePlotStyle.UNSELECTED_LINE_ALPHA)
        pen = QPen(color, LinePlotStyle.UNSELECTED_LINE_WIDTH)
        pen.setCosmetic(True)
        super().__init__(x=x, y=y, pen=pen, pxMode=True, antialias=True)
        self._selected = False
        self._in_subset = False
        self._pen = pen
        self.index = index
        self.id = instance_id
        self.setClickable(True, width=10)
        self.master = master

    def into_subset(self):
        self._in_subset = True
        self._change_pen()

    def out_of_subset(self):
        self._in_subset = False
        self._change_pen()

    def select(self):
        self._selected = True
        self._change_pen()

    def deselect(self):
        self._selected = False
        self._change_pen()

    def setColor(self, color):
        self._pen.setColor(color)
        self._change_pen()

    def reset_pen(self):
        self._change_pen()

    def _change_pen(self):
        pen = QPen(self._pen)
        color = QColor(Qt.black) if self._selected and self.master.has_subset \
            else QColor(self._pen.color())

        if self._in_subset or self._selected:
            width = LinePlotStyle.SELECTED_LINE_WIDTH
            alpha = LinePlotStyle.SELECTED_LINE_ALPHA
        else:
            width = LinePlotStyle.UNSELECTED_LINE_WIDTH
            alpha = LinePlotStyle.UNSELECTED_LINE_ALPHA_WITH_SELECTION \
                if self.master.has_subset or self.master.has_selection else \
                LinePlotStyle.UNSELECTED_LINE_ALPHA

        pen.setWidth(width)
        color.setAlpha(alpha)
        pen.setColor(color)
        self.setPen(pen)


class LinePlotViewBox(ViewBox):
    def __init__(self, graph, enable_menu=False):
        ViewBox.__init__(self, enableMenu=enable_menu)
        self.graph = graph
        self.setMouseMode(self.PanMode)

        pen = mkPen(LinePlotStyle.SELECTION_LINE_COLOR,
                    width=LinePlotStyle.SELECTION_LINE_WIDTH)
        self.selection_line = QGraphicsLineItem()
        self.selection_line.setPen(pen)
        self.selection_line.setZValue(1e9)
        self.addItem(self.selection_line, ignoreBounds=True)

    def update_selection_line(self, button_down_pos, current_pos):
        p1 = self.childGroup.mapFromParent(button_down_pos)
        p2 = self.childGroup.mapFromParent(current_pos)
        self.selection_line.setLine(QLineF(p1, p2))
        self.selection_line.resetTransform()
        self.selection_line.show()

    def mouseDragEvent(self, event, axis=None):
        if self.graph.state == SELECT and axis is None:
            event.accept()
            if event.button() == Qt.LeftButton:
                self.update_selection_line(event.buttonDownPos(), event.pos())
                if event.isFinish():
                    self.selection_line.hide()
                    p1 = self.childGroup.mapFromParent(
                        event.buttonDownPos(event.button()))
                    p2 = self.childGroup.mapFromParent(event.pos())
                    self.graph.select_by_line(p1, p2)
                else:
                    self.update_selection_line(
                        event.buttonDownPos(), event.pos())
        elif self.graph.state == ZOOMING or self.graph.state == PANNING:
            event.ignore()
            super().mouseDragEvent(event, axis=axis)
        else:
            event.ignore()

    def mouseClickEvent(self, event):
        if event.button() == Qt.RightButton:
            self.autoRange()
        else:
            event.accept()
            self.graph.deselect_all()


class LinePlotGraph(pg.PlotWidget):
    def __init__(self, parent):
        super().__init__(parent, viewBox=LinePlotViewBox(self),
                         background="w", enableMenu=False)
        self._items = {}
        self._items_by_id = {}
        self.selection = set()
        self.state = SELECT
        self.master = parent

    def select_by_line(self, p1, p2):
        selection = []
        for item in self.getViewBox().childGroup.childItems():
            if isinstance(item, LinePlotItem):
                if line_segments_line_intersect(p1, p2, item):
                    selection.append(item.index)
        self.select(selection)

    def select_by_click(self, item):
        self.select([item.index])

    def deselect_all(self):
        for i in self.selection:
            self._items[i].deselect()
        self.selection.clear()
        self.master.selection_changed()

    def select(self, indices):
        for i in self.selection:
            self._items[i].deselect()
        keys = QApplication.keyboardModifiers()
        if keys & Qt.ControlModifier:
            self.selection.symmetric_difference_update(indices)
        elif keys & Qt.AltModifier:
            self.selection.difference_update(indices)
        elif keys & Qt.ShiftModifier:
            self.selection.update(indices)
        else:
            self.selection = set(indices)
        for i in self.selection:
            self._items[i].select()
        self.master.selection_changed()

    def select_subset(self, ids):
        for i in ids:
            self._items_by_id[i].into_subset()

    def deselect_subset(self):
        for item in self._items.values():
            item.out_of_subset()

    def reset(self):
        self._items = {}
        self._items_by_id = {}
        self.selection = set()
        self.state = SELECT
        self.clear()
        self.getAxis('bottom').setTicks(None)

    def add_line_plot_item(self, item):
        self._items[item.index] = item
        self._items_by_id[item.id] = item
        self.addItem(item, ignoreBounds=True)

    def finished_adding(self):
        vb = self.getViewBox()
        vb.addedItems.extend(list(self._items.values()))
        vb.updateAutoRange()


class LinePlotDisplay(IntEnum):
    RANGE_WITH_MEAN, INSTANCES, MEAN, INSTANCES_WITH_MEAN = range(4)


class OWLinePlot(OWWidget):
    name = "Line Plot"
    description = "Visualization of data profiles (e.g., time series)."
    icon = "icons/LinePlot.svg"
    priority = 1030

    class Inputs:
        data = Input("Data", Table, default=True)
        data_subset = Input("Data Subset", Table)

    class Outputs:
        selected_data = Output("Selected Data", Table, default=True)
        annotated_data = Output(ANNOTATED_DATA_SIGNAL_NAME, Table)

    settingsHandler = settings.PerfectDomainContextHandler()

    group_var = settings.ContextSetting(None)
    display_index = settings.Setting(LinePlotDisplay.RANGE_WITH_MEAN)
    display_quartiles = settings.Setting(False)
    auto_commit = settings.Setting(True)
    selection = settings.ContextSetting([])

    class Information(OWWidget.Information):
        not_enough_attrs = Msg("Need at least one continuous feature.")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.__groups = None
        self.__profiles = None
        self.data = None
        self.data_subset = None
        self.subset_selection = []
        self.graph_variables = []

        # Setup GUI
        infobox = gui.widgetBox(self.controlArea, "Info")
        self.infoLabel = gui.widgetLabel(infobox, "No data on input.")
        displaybox = gui.widgetBox(self.controlArea, "Display")
        radiobox = gui.radioButtons(displaybox, self, "display_index",
                                    callback=self.__update_visibility)
        gui.appendRadioButton(radiobox, "Range (min-max) with mean")
        gui.appendRadioButton(radiobox, "Line plot")
        gui.appendRadioButton(radiobox, "Mean")
        gui.appendRadioButton(radiobox, "Line plot with mean")

        showbox = gui.widgetBox(self.controlArea, "Show")
        gui.checkBox(showbox, self, "display_quartiles", "Error bars",
                     callback=self.__update_visibility)

        self.group_vars = DomainModel(
            placeholder="None", separators=False,
            valid_types=DiscreteVariable)
        self.group_view = gui.listView(
            self.controlArea, self, "group_var", box="Group by",
            model=self.group_vars, callback=self.__group_var_changed)
        self.group_view.setEnabled(False)
        self.group_view.setMinimumSize(QSize(30, 100))
        self.group_view.setSizePolicy(QSizePolicy.Expanding,
                                      QSizePolicy.Ignored)

        self.gui = OWPlotGUI(self)
        self.box_zoom_select(self.controlArea)

        gui.rubber(self.controlArea)

        gui.auto_commit(self.controlArea, self, "auto_commit",
                        "Send Selection", "Send Automatically")

        self.graph = LinePlotGraph(self)
        self.graph.getPlotItem().buttonsHidden = True
        self.graph.setRenderHint(QPainter.Antialiasing, True)
        self.mainArea.layout().addWidget(self.graph)

        self._legend = LegendItem()
        self._legend.setParentItem(self.graph.getPlotItem().vb)
        self._legend.hide()
        self._legend.anchor((1, 0), (1, 0))

    @property
    def has_selection(self):
        return bool(self.selection)

    @property
    def has_subset(self):
        return bool(self.data_subset)

    def box_zoom_select(self, parent):
        g = self.gui
        box_zoom_select = gui.vBox(parent, "Zoom/Select")
        zoom_select_toolbar = g.zoom_select_toolbar(
            box_zoom_select, nomargin=True,
            buttons=[g.StateButtonsBegin, g.SimpleSelect, g.Pan, g.Zoom,
                     g.StateButtonsEnd, g.ZoomReset]
        )
        buttons = zoom_select_toolbar.buttons
        buttons[g.SimpleSelect].clicked.connect(self.select_button_clicked)
        buttons[g.Pan].clicked.connect(self.pan_button_clicked)
        buttons[g.Zoom].clicked.connect(self.zoom_button_clicked)
        buttons[g.ZoomReset].clicked.connect(self.reset_button_clicked)
        return box_zoom_select

    def select_button_clicked(self):
        self.graph.state = SELECT
        self.graph.getViewBox().setMouseMode(self.graph.getViewBox().RectMode)

    def pan_button_clicked(self):
        self.graph.state = PANNING
        self.graph.getViewBox().setMouseMode(self.graph.getViewBox().PanMode)

    def zoom_button_clicked(self):
        self.graph.state = ZOOMING
        self.graph.getViewBox().setMouseMode(self.graph.getViewBox().RectMode)

    def reset_button_clicked(self):
        self.graph.getViewBox().autoRange()

    def selection_changed(self):
        self.selection = list(self.graph.selection)
        self.__update_visibility()
        self.commit()

    def sizeHint(self):
        return QSize(1000, 562)

    def clear(self):
        """
        Clear/reset the widget state.
        """
        self.__groups = None
        self.__profiles = None
        self.graph_variables = []
        self.graph.reset()
        self.infoLabel.setText("No data on input.")
        self.group_vars.set_domain(None)
        self.group_view.setEnabled(False)
        self._legend.hide()

    @Inputs.data
    def set_data(self, data):
        """
        Set the input profile dataset.
        """
        self.closeContext()
        self.clear()
        self.clear_messages()

        self.data = data
        self.selection = []
        if data is not None:
            self.group_vars.set_domain(data.domain)
            self.group_view.setEnabled(len(self.group_vars) > 1)
            self.group_var = data.domain.class_var if \
                data.domain.class_var and data.domain.class_var.is_discrete \
                else None

            self.infoLabel.setText("%i instances on input\n%i attributes" % (
                len(data), len(data.domain.attributes)))
            self.graph_variables = [var for var in data.domain.attributes
                                    if var.is_continuous]
            if len(self.graph_variables) < 1:
                self.data = None
                self.Information.not_enough_attrs()
                self.commit()
                return

        self.openContext(data)
        self._setup_plot()
        self.commit()

    @Inputs.data_subset
    def set_data_subset(self, subset):
        """
        Set the supplementary input subset dataset.
        """
        self.data_subset = subset
        if len(self.subset_selection):
            self.graph.deselect_subset()

    def handleNewSignals(self):
        self.subset_selection = []
        if self.data is not None and self.has_subset and \
                len(self.graph_variables):
            intersection = set(self.data.ids).intersection(
                set(self.data_subset.ids))
            self.subset_selection = intersection
            if self.__profiles is not None:
                self.graph.select_subset(self.subset_selection)
        if self.data is not None:
            self.__update_visibility()

    def _setup_plot(self):
        """Setup the plot with new curve data."""
        if self.data is None:
            return

        ticks = [[(i + 1, str(a)) for i, a in enumerate(self.graph_variables)]]
        self.graph.getAxis('bottom').setTicks(ticks)
        self.graph.getViewBox().enableAutoRange()
        if self.display_index in (LinePlotDisplay.INSTANCES,
                                  LinePlotDisplay.INSTANCES_WITH_MEAN):
            self._plot_profiles()
        self._plot_groups()
        self.__update_visibility()
        self._setup_legend()

    def _setup_legend(self):
        self._legend.clear()
        self._legend.hide()
        if self.group_var:
            for index, name in enumerate(self.group_var.values):
                color = self.__get_line_color(None, index)
                self._legend.addItem(
                    ScatterPlotItem(pen=color, brush=color, size=10, shape="s"),
                    escape(name)
                )
            self._legend.show()

    def _plot_profiles(self):
        X = np.arange(1, len(self.graph_variables) + 1)
        data = self.data[:, self.graph_variables]
        self.__profiles = []
        for index, inst in zip(range(len(self.data)), data):
            color = self.__get_line_color(index)
            profile = LinePlotItem(self, index, inst.id, X, inst.x, color)
            profile.sigClicked.connect(self.graph.select_by_click)
            self.graph.add_line_plot_item(profile)
            self.__profiles.append(profile)
        self.graph.finished_adding()
        self.__select_data_instances()

    def _plot_groups(self):
        if self.__groups is not None:
            for group in self.__groups:
                if group is not None:
                    self.graph.getViewBox().removeItem(group.mean)
                    self.graph.getViewBox().removeItem(group.range)
                    self.graph.getViewBox().removeItem(group.error_bar)

        self.__groups = []
        X = np.arange(1, len(self.graph_variables) + 1)
        if self.group_var is None:
            self.__plot_group(X, self.data[:, self.graph_variables])
        else:
            class_col_data, _ = self.data.get_column_view(self.group_var)
            group_indices = [np.flatnonzero(class_col_data == i)
                             for i in range(len(self.group_var.values))]
            for index, indices in enumerate(group_indices):
                if len(indices) == 0:
                    self.__groups.append(None)
                else:
                    group_data = self.data[indices, self.graph_variables]
                    self.__plot_group(X, group_data, index)

    def __plot_group(self, X, data, index=None):
        p = QPen(self.__get_line_color(None, index), LinePlotStyle.MEAN_WIDTH)
        p.setCosmetic(True)
        mean = np.nanmean(data.X, axis=0)
        mean_curve = self.get_mean_curve(X, mean, p)
        range_curve = self.get_range_curve(X, data, p)
        error_bar = self.get_error_bar(X, data, mean)
        self.graph.addItem(mean_curve)
        self.graph.addItem(range_curve)
        self.graph.addItem(error_bar)
        self.__groups.append(namespace(mean=mean_curve,
                                       range=range_curve,
                                       error_bar=error_bar))

    @staticmethod
    def get_mean_curve(X, mean, pen):
        return pg.PlotDataItem(x=X, y=mean, pen=pen, antialias=True)

    @staticmethod
    def get_range_curve(X, data, pen):
        color = QColor(pen.color())
        color.setAlpha(LinePlotStyle.RANGE_ALPHA)
        max_curve = pg.PlotDataItem(x=X, y=np.nanmax(data.X, axis=0))
        min_curve = pg.PlotDataItem(x=X, y=np.nanmin(data.X, axis=0))
        return pg.FillBetweenItem(max_curve, min_curve, brush=color)

    @staticmethod
    def get_error_bar(X, data, mean):
        q1, q2, q3 = np.nanpercentile(data.X, [25, 50, 75], axis=0)
        bottom = np.clip(mean - q1, 0, mean - q1)
        top = np.clip(q3 - mean, 0, q3 - mean)
        return pg.ErrorBarItem(x=X, y=mean, bottom=bottom, top=top, beam=0.01)

    def __update_visibility(self):
        self.__update_visibility_profiles()
        self.__update_visibility_groups()

    def __update_visibility_groups(self):
        show_mean = self.display_index in (LinePlotDisplay.MEAN,
                                           LinePlotDisplay.INSTANCES_WITH_MEAN,
                                           LinePlotDisplay.RANGE_WITH_MEAN)
        show_range = self.display_index == LinePlotDisplay.RANGE_WITH_MEAN
        if self.__groups is not None:
            for group in self.__groups:
                if group is not None:
                    group.mean.setVisible(show_mean)
                    group.range.setVisible(show_range)
                    group.error_bar.setVisible(self.display_quartiles)

    def __update_visibility_profiles(self):
        show_selection = self.display_index == LinePlotDisplay.RANGE_WITH_MEAN
        show_inst = self.display_index in (LinePlotDisplay.INSTANCES,
                                           LinePlotDisplay.INSTANCES_WITH_MEAN)
        if self.__profiles is None and (show_inst or show_selection) \
                and self.data is not None:
            self._plot_profiles()
            self.graph.select_subset(self.subset_selection)
        if self.__profiles is not None:
            selection = set(chain(
                self.selection,
                self.data_subset.ids if self.has_subset else []
            ))
            for profile in self.__profiles:
                selected = profile.index in selection
                profile.setVisible(show_inst or selected and show_selection)
                profile.reset_pen()

    def __group_var_changed(self):
        if self.data is None or not len(self.graph_variables):
            return
        self.__color_profiles()
        self._plot_groups()
        self.__update_visibility()
        self._setup_legend()

    def __color_profiles(self):
        if self.__profiles is not None:
            for profile in self.__profiles:
                profile.setColor(self.__get_line_color(profile.index))

    def __select_data_instances(self):
        if self.data is None or not len(self.data) or not self.has_selection:
            return
        if max(self.selection) >= len(self.data):
            self.selection = []
        self.graph.select(self.selection)

    def __get_line_color(self, data_index=None, mean_index=None):
        color = QColor(LinePlotStyle.DEFAULT_COLOR)
        if self.group_var is not None:
            if data_index is not None:
                value = self.data[data_index][self.group_var]
                if np.isnan(value):
                    return color
            index = int(value) if data_index is not None else mean_index
            color = LinePlotStyle()(len(self.group_var.values))[index]
        return color.darker(110) if data_index is None else color

    def commit(self):
        selected = self.data[self.selection] \
            if self.data is not None and self.has_selection else None
        annotated = create_annotated_table(self.data, self.selection)
        self.Outputs.selected_data.send(selected)
        self.Outputs.annotated_data.send(annotated)


def test_main(argv=sys.argv):
    from AnyQt.QtWidgets import QApplication
    a = QApplication(argv)
    if len(argv) > 1:
        filename = argv[1]
    else:
        filename = "brown-selected"
    w = OWLinePlot()
    d = Table(filename)

    w.set_data(d)
    w.show()
    r = a.exec_()
    w.saveSettings()
    return r


if __name__ == "__main__":
    sys.exit(test_main())
