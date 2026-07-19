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

    // The daemon precomputes state.json, so a refresh is a 2ms `cat` rather
    // than a 33ms Python run, and the totals are live rather than lagging the
    // database flush.
    readonly property int refreshMs: 1000

    property var totals: ({})
    property var activity: ({})
    property var peak: null
    property real updated: 0
    property string activeSource: ""
    property bool loading: false
    property string lastError: ""

    readonly property bool available: updated > 0 && lastError.length === 0
    // The daemon heartbeats every 30s whether or not anything was typed, so a
    // gap this long means it died rather than that you sat still.
    readonly property bool stale: available && (Date.now() / 1000 - updated) > 90

    // Same total the popup headline shows, so panel and popup never disagree.
    readonly property real clicks: totalClicks()
    readonly property real keys: metric("keystrokes")

    Plasmoid.title: i18n("Click Analytics")
    Plasmoid.icon: "input-mouse"
    Plasmoid.status: PlasmaCore.Types.ActiveStatus
    Plasmoid.backgroundHints: PlasmaCore.Types.NoBackground

    toolTipMainText: i18n("Click Analytics")
    toolTipSubText: available
        ? i18n("%1 clicks · %2 keystrokes", formatFull(clicks), formatFull(keys))
        : i18n("Daemon not running")

    compactRepresentation: MouseArea {
        Layout.minimumWidth: counters.implicitWidth + Kirigami.Units.smallSpacing * 2
        Layout.preferredWidth: Layout.minimumWidth
        Layout.minimumHeight: Kirigami.Units.iconSizes.small * 2

        onClicked: root.expanded = !root.expanded

        CompactCounters {
            id: counters
            anchors.fill: parent
            clicks: root.clicks
            keys: root.keys
            available: root.available
            stale: root.stale
        }
    }

    fullRepresentation: PlasmaExtras.Representation {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 21
        Layout.minimumHeight: Kirigami.Units.gridUnit * 24
        collapseMarginsHint: true

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: Kirigami.Units.largeSpacing
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents3.Label {
                text: i18n("Input Analytics")
                font.bold: true
                Layout.fillWidth: true
            }

            PlasmaComponents3.Label {
                visible: !root.available || root.stale
                text: root.stale
                    ? i18n("Counts look stale — the daemon may have stopped.")
                    : i18n("Daemon not running. Check: systemctl --user status kdeclickd")
                color: root.stale ? Kirigami.Theme.neutralTextColor : Kirigami.Theme.negativeTextColor
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // Mouse first here and in the panel, matching the widget's name.
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                spacing: Kirigami.Units.largeSpacing

                Repeater {
                    model: [
                        {"icon": "input-mouse", "value": root.totalClicks(), "label": i18n("clicks")},
                        {"icon": "input-keyboard", "value": root.keys, "label": i18n("keystrokes")}
                    ]

                    RowLayout {
                        required property var modelData

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
                    required property var modelData

                    // Side and extra buttons only appear once used, so the
                    // popup does not list rows that are structurally always 0.
                    visible: root.metric(modelData.key) > 0
                        || modelData.key === "click_left"
                        || modelData.key === "click_right"
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
                    text: i18n("Scroll")
                    opacity: 0.75
                    Layout.fillWidth: true
                }
                PlasmaComponents3.Label {
                    // Wheel and touchpad are stored separately because they are
                    // not the same unit; the equivalence is applied here so the
                    // ratio can be retuned without rewriting history.
                    text: i18n("%1 notches", root.formatFull(root.totalScrollNotches()))
                    PlasmaComponents3.ToolTip.text: i18n(
                        "%1 wheel notches + %2 touchpad events at %3 per notch",
                        root.formatFull(root.metric("scroll_wheel")),
                        root.formatFull(root.metric("scroll_touchpad")),
                        Plasmoid.configuration.touchpadPerNotch)
                    PlasmaComponents3.ToolTip.visible: scrollHover.hovered
                    PlasmaComponents3.ToolTip.delay: 300

                    HoverHandler {
                        id: scrollHover
                    }
                }

                PlasmaComponents3.Label {
                    text: i18n("Pointer travel")
                    opacity: 0.75
                    Layout.fillWidth: true
                }
                PlasmaComponents3.Label {
                    // Depends on the DPI setting, so the tooltip names it rather
                    // than presenting the distance as measured fact.
                    text: root.travelText()
                    PlasmaComponents3.ToolTip.text: i18n("Estimated using %1 DPI (configurable)", Plasmoid.configuration.mouseDpi)
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
                values: (root.activity || {}).values || []
                labels: (root.activity || {}).labels || []
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.gridUnit * 3
            }

            PlasmaComponents3.Label {
                visible: Boolean(root.peak)
                text: root.peak
                    ? i18n("Peak %1:00 · %2 actions", root.peak.hour, root.formatFull(root.peak.value))
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
                // cat failed, so the file is missing: the daemon has not
                // written state yet, or is not running.
                root.updated = 0;
                root.lastError = "";
                return;
            }
            try {
                const parsed = JSON.parse(stdout);
                root.totals = parsed.totals || {};
                root.activity = parsed.activity || {};
                root.peak = parsed.peak || null;
                root.updated = parsed.updated || 0;
                root.lastError = "";
            } catch (error) {
                root.lastError = i18n("Could not parse daemon state file.");
            }
        }
    }

    Timer {
        interval: root.refreshMs
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refreshData()
    }

    function refreshData() {
        if (loading) {
            return;
        }
        // Same XDG resolution the daemon uses, so the two cannot disagree
        // about where state lives.
        activeSource = 'cat "${XDG_DATA_HOME:-$HOME/.local/share}/kdeclick/state.json"';
        loading = true;
        executable.connectSource(activeSource);
    }

    function metric(key) {
        return (totals && totals[key]) || 0;
    }

    function totalClicks() {
        return metric("click_left") + metric("click_right") + metric("click_middle")
            + metric("click_side") + metric("click_extra");
    }

    function totalScrollNotches() {
        var perNotch = Plasmoid.configuration.touchpadPerNotch;
        return metric("scroll_wheel") + metric("scroll_touchpad") / perNotch;
    }

    function maxClick() {
        return Math.max(1, metric("click_left"), metric("click_right"),
            metric("click_middle"), metric("click_side"), metric("click_extra"));
    }

    function travelText() {
        var metres = metric("motion_units") / Plasmoid.configuration.mouseDpi * 0.0254;
        // Two decimals is centimetre resolution. Note this is precision, not
        // accuracy: the DPI assumption dominates the error by far.
        if (metres >= 1000) {
            return i18n("%1 km", (metres / 1000).toFixed(3));
        }
        return i18n("%1 m", metres.toFixed(2));
    }

    function formatFull(value) {
        // Explicit 'f',0: Qt's toLocaleString defaults to 2 decimals, which
        // renders every whole-number count as "289,713.00".
        return Math.round(value).toLocaleString(Qt.locale(), 'f', 0);
    }
}
