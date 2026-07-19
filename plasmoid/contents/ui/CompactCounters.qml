import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

// Stacked rather than side by side: two short rows cost far less panel width
// than one long row, which matters on a crowded horizontal panel.
Item {
    id: counters

    property real clicks: 0
    property real keys: 0
    property bool available: true
    property bool stale: false

    // Panels are short, so the font is derived from the row height rather than
    // fixed; at 44px this lands around 14px per row.
    readonly property real rowHeight: Math.max(8, height / 2 - 1)
    readonly property real fontSize: Math.max(6, Math.min(rowHeight * 0.72, Kirigami.Theme.defaultFont.pixelSize))

    implicitWidth: layout.implicitWidth
    implicitHeight: Kirigami.Units.iconSizes.small * 2

    function format(value) {
        // Full number with separators, matching the popup. Explicit 'f',0
        // because Qt's toLocaleString defaults to two decimal places.
        return Math.round(value).toLocaleString(Qt.locale(), 'f', 0);
    }

    ColumnLayout {
        id: layout
        anchors.centerIn: parent
        spacing: 0

        Repeater {
            model: [
                {"icon": "input-mouse", "value": counters.clicks},
                {"icon": "input-keyboard", "value": counters.keys}
            ]

            RowLayout {
                required property var modelData

                // Rows size to their content and the column centres itself.
                // Forcing each row to half the panel height leaves the two
                // numbers drifting apart on tall panels, since the font is
                // capped well below half of a 76px panel.
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Icon {
                    source: modelData.icon
                    Layout.preferredWidth: counters.fontSize
                    Layout.preferredHeight: counters.fontSize
                    opacity: counters.available ? 0.85 : 0.4
                }

                PlasmaComponents3.Label {
                    text: counters.available ? counters.format(modelData.value) : "—"
                    font.pixelSize: counters.fontSize
                    font.features: ({ "tnum": 1 })
                    color: counters.stale ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
                    Layout.alignment: Qt.AlignVCenter
                }
            }
        }
    }
}
