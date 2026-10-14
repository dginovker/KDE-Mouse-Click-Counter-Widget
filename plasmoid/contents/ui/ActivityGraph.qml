import QtQuick
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

Item {
    id: graph

    property var values: []
    property var secondaryValues: []
    property var labels: []
    property color primaryColor: Kirigami.Theme.highlightColor
    property color secondaryColor: Kirigami.Theme.positiveTextColor
    property var tooltipText: null

    readonly property bool stacked: secondaryValues.length === values.length
        && values.length > 0

    readonly property real maximum: {
        var peak = 0;
        for (var i = 0; i < values.length; i++) {
            peak = Math.max(peak, values[i] + (stacked ? secondaryValues[i] : 0));
        }
        return peak;
    }

    function barHeight(value) {
        return maximum > 0 ? value / maximum * bars.height : 0;
    }

    function baselineColor() {
        return Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
            Kirigami.Theme.textColor.b, 0.18);
    }

    function tooltipFor(index) {
        if (labels.length <= index) {
            return "";
        }
        return tooltipText ? tooltipText(index)
            : i18n("%1:00 — %2 actions", labels[index], Math.round(values[index]));
    }

    Row {
        id: bars
        anchors.fill: parent
        anchors.bottomMargin: hourLabels.height
        spacing: 1

        Repeater {
            model: graph.values.length

            Item {
                id: hour

                width: (graph.width - (graph.values.length - 1)) / graph.values.length
                height: bars.height
                activeFocusOnTab: true
                Accessible.role: Accessible.StaticText
                Accessible.name: graph.tooltipFor(index)

                Rectangle {
                    id: primaryBar
                    anchors.bottom: parent.bottom
                    width: parent.width
                    // Only empty hours need a baseline; zero segments must not inflate a stack.
                    height: graph.values[index] > 0 || (graph.stacked && graph.secondaryValues[index] > 0)
                        ? graph.barHeight(graph.values[index]) : 1
                    radius: width > 3 ? 1 : 0
                    color: graph.values[index] > 0
                        ? graph.primaryColor : graph.baselineColor()

                    Behavior on height {
                        NumberAnimation {
                            duration: Kirigami.Units.longDuration
                            easing.type: Easing.OutCubic
                        }
                    }
                }

                Rectangle {
                    visible: graph.stacked
                    anchors.bottom: primaryBar.top
                    width: parent.width
                    height: graph.stacked
                        ? graph.barHeight(graph.secondaryValues[index]) : 0
                    radius: width > 3 ? 1 : 0
                    color: graph.stacked && graph.secondaryValues[index] > 0
                        ? graph.secondaryColor : graph.baselineColor()

                    Behavior on height {
                        NumberAnimation {
                            duration: Kirigami.Units.longDuration
                            easing.type: Easing.OutCubic
                        }
                    }
                }

                HoverHandler {
                    id: hover
                }

                TapHandler {
                    onTapped: hour.forceActiveFocus()
                }

                PlasmaComponents3.ToolTip.text: graph.tooltipFor(index)
                PlasmaComponents3.ToolTip.visible: hover.hovered || hour.activeFocus
                PlasmaComponents3.ToolTip.delay: 200
            }
        }
    }

    Row {
        id: hourLabels
        anchors.bottom: parent.bottom
        width: parent.width
        height: Kirigami.Theme.smallFont.pixelSize + 2
        spacing: 1

        Repeater {
            model: graph.labels.length

            PlasmaComponents3.Label {
                width: (graph.width - (graph.labels.length - 1)) / graph.labels.length
                horizontalAlignment: Text.AlignHCenter
                // Every sixth hour only; 24 labels never fit the popup width.
                text: (index % 6 === 0) ? graph.labels[index] : ""
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize * 0.85
                opacity: 0.55
            }
        }
    }
}
