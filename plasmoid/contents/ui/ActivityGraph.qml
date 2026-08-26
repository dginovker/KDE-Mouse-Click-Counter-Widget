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

    readonly property bool paired: secondaryValues.length === values.length
        && values.length > 0

    readonly property real maximum: {
        var peak = 0;
        for (var i = 0; i < values.length; i++) {
            peak = Math.max(peak, values[i]);
            if (paired) {
                peak = Math.max(peak, secondaryValues[i]);
            }
        }
        return peak;
    }

    function barHeight(value) {
        return Math.max(1, maximum > 0 ? value / maximum * bars.height : 1);
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
                    anchors.bottom: parent.bottom
                    anchors.left: parent.left
                    width: graph.paired ? (parent.width - 1) / 2 : parent.width
                    // A floor of 1px keeps empty hours visible as a baseline, so
                    // "no activity" reads as a measured zero rather than a gap
                    // where the graph failed to draw.
                    height: graph.barHeight(graph.values[index])
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
                    visible: graph.paired
                    anchors.bottom: parent.bottom
                    anchors.right: parent.right
                    width: (parent.width - 1) / 2
                    height: graph.paired
                        ? graph.barHeight(graph.secondaryValues[index]) : 1
                    radius: width > 3 ? 1 : 0
                    color: graph.paired && graph.secondaryValues[index] > 0
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
