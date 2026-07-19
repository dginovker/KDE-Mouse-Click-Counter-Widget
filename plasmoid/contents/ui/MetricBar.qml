import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

RowLayout {
    id: bar

    property string label: ""
    property real value: 0
    property real maximum: 1
    property string valueText: ""

    spacing: Kirigami.Units.smallSpacing

    PlasmaComponents3.Label {
        text: bar.label
        opacity: 0.75
        Layout.preferredWidth: Kirigami.Units.gridUnit * 3.2
    }

    PlasmaComponents3.Label {
        text: bar.valueText
        horizontalAlignment: Text.AlignRight
        // Parenthesised: a bare {...} in a binding parses as a code block,
        // not an object literal, so tabular figures would silently not apply.
        font.features: ({ "tnum": 1 })
        Layout.preferredWidth: Kirigami.Units.gridUnit * 3.5
    }

    Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: Kirigami.Units.gridUnit * 0.55
        radius: height / 2
        color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.10)

        Rectangle {
            width: parent.width * Math.min(1, bar.maximum > 0 ? bar.value / bar.maximum : 0)
            height: parent.height
            radius: parent.radius
            color: Kirigami.Theme.highlightColor

            Behavior on width {
                NumberAnimation {
                    duration: Kirigami.Units.longDuration
                    easing.type: Easing.OutCubic
                }
            }
        }
    }
}
