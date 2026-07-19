import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasma5support as P5Support
import org.kde.plasma.plasmoid

PlasmoidItem {
    id: root

    readonly property int refreshMs: 10 * 1000
    readonly property string helperPath: fileUrlToPath(Qt.resolvedUrl("../code/stats_snapshot.py"))
    readonly property var windows: [{"key": "today", "label": i18n("Today")}, {"key": "week", "label": i18n("Week")}, {"key": "all", "label": i18n("All time")}]

    property string activeWindow: "today"
    property string activeSource: ""
    property var snapshot: ({})
    property bool loading: false
    property string lastError: ""

    readonly property var panelTotals: totalsFor(Plasmoid.configuration.panelWindow || "all")
    readonly property real panelClicks: panelTotals.click_left || 0
    readonly property real panelKeys: panelTotals.keystrokes || 0

    Plasmoid.title: i18n("Click Analytics")
    Plasmoid.icon: "input-mouse"
    Plasmoid.status: PlasmaCore.Types.ActiveStatus
    Plasmoid.backgroundHints: PlasmaCore.Types.NoBackground

    toolTipMainText: i18n("Click Analytics")
    toolTipSubText: snapshot.available === false
        ? i18n("Daemon not running")
        : i18n("%1 clicks · %2 keystrokes", formatFull(panelClicks), formatFull(panelKeys))

    compactRepresentation: MouseArea {
        Layout.minimumWidth: counters.implicitWidth + Kirigami.Units.smallSpacing * 2
        Layout.preferredWidth: Layout.minimumWidth
        Layout.minimumHeight: Kirigami.Units.iconSizes.small * 2

        onClicked: {
            root.refreshData();
            root.expanded = !root.expanded;
        }

        CompactCounters {
            id: counters
            anchors.fill: parent
            clicks: root.panelClicks
            keys: root.panelKeys
            available: root.snapshot.available !== false
            stale: root.snapshot.stale === true
        }
    }

    fullRepresentation: PlasmaExtras.Representation {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 21
        Layout.minimumHeight: Kirigami.Units.gridUnit * 26
        collapseMarginsHint: true

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: Kirigami.Units.largeSpacing
            spacing: Kirigami.Units.smallSpacing

            RowLayout {
                Layout.fillWidth: true

                PlasmaComponents3.Label {
                    text: i18n("Input Analytics")
                    font.bold: true
                    Layout.fillWidth: true
                }

                Repeater {
                    model: root.windows

                    Rectangle {
                        Layout.preferredWidth: chipLabel.implicitWidth + Kirigami.Units.largeSpacing
                        Layout.preferredHeight: Kirigami.Units.gridUnit * 1.3
                        radius: Kirigami.Units.cornerRadius
                        color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, root.activeWindow === modelData.key ? 0.16 : 0.07)
                        border.width: root.activeWindow === modelData.key ? 1 : 0
                        border.color: Kirigami.Theme.highlightColor

                        PlasmaComponents3.Label {
                            id: chipLabel
                            anchors.centerIn: parent
                            text: modelData.label
                            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                            font.bold: root.activeWindow === modelData.key
                        }

                        MouseArea {
                            anchors.fill: parent
                            onClicked: root.activeWindow = modelData.key
                        }
                    }
                }
            }

            PlasmaComponents3.Label {
                visible: root.snapshot.available === false || root.lastError.length > 0
                text: root.lastError.length > 0 ? root.lastError : (root.snapshot.error || "")
                color: Kirigami.Theme.negativeTextColor
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
            }

            PlasmaComponents3.Label {
                visible: root.snapshot.stale === true
                text: i18n("Counts look stale — the daemon may have stopped.")
                color: Kirigami.Theme.neutralTextColor
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                spacing: Kirigami.Units.largeSpacing

                Repeater {
                    model: [
                        {"icon": "input-keyboard", "value": root.metric("keystrokes"), "label": i18n("keystrokes")},
                        {"icon": "input-mouse", "value": root.totalClicks(), "label": i18n("clicks")}
                    ]

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing

                        Kirigami.Icon {
                            source: modelData.icon
                            Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium
                            Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium
                        }

                        ColumnLayout {
                            spacing: 0

                            PlasmaComponents3.Label {
                                text: root.formatFull(modelData.value)
                                font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.3
                                font.bold: true
                            }

                            PlasmaComponents3.Label {
                                text: modelData.label
                                opacity: 0.7
                                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                            }
                        }
                    }
                }
            }

            Kirigami.Separator {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
            }

            Repeater {
                model: [
                    {"key": "click_left", "label": i18n("Left")},
                    {"key": "click_right", "label": i18n("Right")},
                    {"key": "click_middle", "label": i18n("Middle")},
                    {"key": "click_side", "label": i18n("Side")},
                    {"key": "click_extra", "label": i18n("Extra")}
                ]

                MetricBar {
                    visible: root.metric(modelData.key) > 0 || modelData.key === "click_left" || modelData.key === "click_right"
                    label: modelData.label
                    value: root.metric(modelData.key)
                    maximum: root.maxClick()
                    valueText: root.formatFull(root.metric(modelData.key))
                    Layout.fillWidth: true
                }
            }

            Kirigami.Separator {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
            }

            GridLayout {
                columns: 2
                Layout.fillWidth: true
                columnSpacing: Kirigami.Units.largeSpacing
                rowSpacing: 2

                PlasmaComponents3.Label {
                    text: i18n("Wheel scroll")
                    opacity: 0.75
                    Layout.fillWidth: true
                }
                PlasmaComponents3.Label {
                    text: i18n("%1 notches", root.formatFull(root.metric("scroll_wheel")))
                }

                PlasmaComponents3.Label {
                    text: i18n("Touchpad scroll")
                    opacity: 0.75
                    Layout.fillWidth: true
                }
                PlasmaComponents3.Label {
                    text: root.formatFull(root.metric("scroll_touchpad"))
                }

                PlasmaComponents3.Label {
                    text: i18n("Pointer travel")
                    opacity: 0.75
                    Layout.fillWidth: true
                }
                PlasmaComponents3.Label {
                    text: root.travelText()
                    // The conversion assumes a DPI, so the tooltip names it
                    // rather than presenting the distance as measured fact.
                    PlasmaComponents3.ToolTip.text: i18n("Estimated using %1 DPI (configurable)", Plasmoid.configuration.mouseDpi || 800)
                    PlasmaComponents3.ToolTip.visible: travelHover.hovered
                    PlasmaComponents3.ToolTip.delay: 300

                    HoverHandler {
                        id: travelHover
                    }
                }
            }

            PlasmaComponents3.Label {
                text: i18n("Activity (24h)")
                font.bold: true
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
            }

            ActivityGraph {
                values: (root.snapshot.activity || {}).values || []
                labels: (root.snapshot.activity || {}).labels || []
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.gridUnit * 3
            }

            PlasmaComponents3.Label {
                visible: Boolean(root.snapshot.peak)
                text: root.snapshot.peak
                    ? i18n("Peak %1:00 · %2 actions", root.snapshot.peak.hour, root.formatFull(root.snapshot.peak.value))
                    : ""
                opacity: 0.7
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                Layout.fillWidth: true
            }

            Item {
                Layout.fillHeight: true
            }
        }
    }

    P5Support.DataSource {
        id: executable
        engine: "executable"

        onNewData: function (sourceName, data) {
            if (sourceName !== root.activeSource) {
                return;
            }
            disconnectSource(sourceName);
            root.activeSource = "";
            root.loading = false;

            const stdout = data.stdout || "";
            if (stdout.length === 0) {
                root.lastError = i18n("Stats helper returned no data.");
                return;
            }
            try {
                root.snapshot = JSON.parse(stdout);
                root.lastError = "";
            } catch (error) {
                root.lastError = i18n("Could not parse stats helper output.");
            }
        }
    }

    Timer {
        interval: root.refreshMs
        running: true
        repeat: true
        onTriggered: root.refreshData()
    }

    Component.onCompleted: refreshData()

    function fileUrlToPath(url) {
        var text = url.toString();
        if (text.indexOf("file://") === 0) {
            return decodeURIComponent(text.substring(7));
        }
        return text;
    }

    function shellQuote(text) {
        return "'" + text.replace(/'/g, "'\\''") + "'";
    }

    function refreshData() {
        if (loading) {
            return;
        }
        activeSource = "python3 " + shellQuote(helperPath) + " --stamp " + Date.now();
        loading = true;
        executable.connectSource(activeSource);
    }

    function totalsFor(windowKey) {
        if (!snapshot || typeof snapshot !== "object") {
            return {};
        }
        return snapshot[windowKey] || {};
    }

    function metric(key) {
        var totals = totalsFor(activeWindow);
        return totals[key] || 0;
    }

    function totalClicks() {
        return metric("click_left") + metric("click_right") + metric("click_middle")
            + metric("click_side") + metric("click_extra");
    }

    function maxClick() {
        return Math.max(1, metric("click_left"), metric("click_right"),
            metric("click_middle"), metric("click_side"), metric("click_extra"));
    }

    function travelText() {
        var dpi = Plasmoid.configuration.mouseDpi || 800;
        var metres = metric("motion_units") / dpi * 0.0254;
        if (metres >= 1000) {
            return i18n("%1 km", (metres / 1000).toFixed(1));
        }
        return i18n("%1 m", metres.toFixed(metres < 10 ? 1 : 0));
    }

    function formatShort(value) {
        if (value >= 1e6) {
            return (value / 1e6).toFixed(value >= 1e7 ? 0 : 1) + "M";
        }
        if (value >= 1000) {
            return (value / 1000).toFixed(value >= 10000 ? 0 : 1) + "k";
        }
        return Math.round(value).toString();
    }

    function formatFull(value) {
        // Explicit 'f',0: Qt's toLocaleString defaults to 2 decimals, which
        // renders every whole-number count as "289,713.00".
        return Math.round(value).toLocaleString(Qt.locale(), 'f', 0);
    }
}
