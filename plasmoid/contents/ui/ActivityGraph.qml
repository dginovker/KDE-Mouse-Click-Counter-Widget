import QtQuick
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

Item {
    id: graph

    property var values: []
    property var labels: []

    readonly property real maximum: {
        var peak = 0;
        for (var i = 0; i < values.length; i++) {
            peak = Math.max(peak, values[i]);
        }
        return peak;
    }

    Row {
        id: bars
        anchors.fill: parent
        anchors.bottomMargin: hourLabels.height
        spacing: 1

        Repeater {
            model: graph.values.length

            Item {
                width: (graph.width - (graph.values.length - 1)) / graph.values.length
                height: bars.height

                Rectangle {
                    anchors.bottom: parent.bottom
                    width: parent.width
                    // A floor of 1px keeps empty hours visible as a baseline, so
                    // "no activity" reads as a measured zero rather than a gap
                    // where the graph failed to draw.
                    height: Math.max(1, graph.maximum > 0
                        ? (graph.values[index] / graph.maximum) * parent.height
                        : 1)
                    radius: width > 3 ? 1 : 0
                    color: graph.values[index] > 0
                        ? Kirigami.Theme.highlightColor
                        : Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.18)
                    opacity: graph.maximum > 0 && graph.values[index] > 0
                        ? 0.45 + 0.55 * (graph.values[index] / graph.maximum)
                        : 1

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

                PlasmaComponents3.ToolTip.text: graph.labels.length > index
                    ? i18n("%1:00 — %2 actions", graph.labels[index], Math.round(graph.values[index]))
                    : ""
                PlasmaComponents3.ToolTip.visible: hover.hovered
                PlasmaComponents3.ToolTip.delay: 200
            }
        }
    }

    Row {
        id: hourLabels
        anchors.bottom: parent.bottom
        width: parent.width
        height: Kirigami.Theme.smallFont.pixelSize + 2

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
