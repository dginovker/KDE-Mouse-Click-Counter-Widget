import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasmoid
import org.kde.plasma.workspace.dbus as DBus

PlasmoidItem {
    id: root

    readonly property string dbusService: "io.github.dginovker.KDEClickAnalytics"
    readonly property string dbusPath: "/io/github/dginovker/KDEClickAnalytics"
    readonly property string dbusInterface: "io.github.dginovker.KDEClickAnalytics1"

    property var snapshot: null
    readonly property var totals: snapshot ? snapshot.totals : ({})
    readonly property var activity: snapshot ? snapshot.activity : ({})
    readonly property var peak: snapshot ? snapshot.peak : null
    readonly property var network: snapshot ? snapshot.network
        : ({"status": "initializing", "interface": "", "sampled_at": 0, "error": ""})
    readonly property real updated: snapshot ? snapshot.updated : 0
    property real nowSeconds: Date.now() / 1000
    property bool componentReady: false
    property int serviceEpoch: 0
    property string acceptedInstance: ""
    property double acceptedRevision: -1
    property string lastError: i18n("The daemon's D-Bus service is not registered. Check: systemctl --user status kdeclickd")

    // Network sampling publishes once per second; a long margin avoids a false
    // alarm while the machine is resuming from sleep.
    readonly property bool hasSnapshot: snapshot !== null
    readonly property bool stale: hasSnapshot && nowSeconds - updated > 90
    readonly property bool available: hasSnapshot && serviceWatcher.registered
        && lastError.length === 0 && !stale
    readonly property string daemonStatus: lastError || (stale
        ? i18n("The last daemon update is stale; its service may be stuck.")
        : i18n("No valid daemon state has been received."))
    readonly property string networkStatus: network.status === "ok"
        ? i18n("Interface %1 · sampled %2 s ago", network.interface,
            Math.max(0, Math.floor(nowSeconds - network.sampled_at)))
        : network.status === "initializing"
            ? i18n("Network measurement is initializing.")
            : i18n("Network measurement %1: %2", network.status, network.error)

    // Same total the popup headline shows, so panel and popup never disagree.
    readonly property real clicks: totalClicks()
    readonly property real keys: metric("keystrokes")

    Plasmoid.title: i18n("Click Analytics")
    Plasmoid.icon: "input-mouse"
    Plasmoid.status: PlasmaCore.Types.ActiveStatus
    Plasmoid.backgroundHints: PlasmaCore.Types.NoBackground

    toolTipMainText: i18n("Click Analytics")
    toolTipSubText: hasSnapshot
        ? i18n("%1 clicks · %2 keystrokes", formatFull(clicks), formatFull(keys))
            + (available ? "" : "\n" + daemonStatus)
        : daemonStatus

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
            // Preserve the last trustworthy totals during a daemon fault; the
            // red state and tooltip make their age explicit.
            available: root.hasSnapshot
            stale: !root.available
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
                visible: !root.available
                text: root.daemonStatus
                color: Kirigami.Theme.negativeTextColor
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

                PlasmaComponents3.Label {
                    text: i18n("Downloaded %1 · Uploaded %2",
                        root.formatBytes(root.metric("network_rx_bytes")),
                        root.formatBytes(root.metric("network_tx_bytes"))) + "\n" + root.networkStatus
                    color: root.network.status === "initializing"
                        || root.network.status === "ok"
                        ? Kirigami.Theme.neutralTextColor : Kirigami.Theme.negativeTextColor
                    wrapMode: Text.WordWrap
                    Layout.columnSpan: 2
                    Layout.fillWidth: true
                }
            }

            PlasmaComponents3.Label {
                text: i18n("Activity (24h)")
                font.bold: true
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
            }

            ActivityGraph {
                values: root.hasSnapshot ? root.activity.values : []
                labels: root.hasSnapshot ? root.activity.labels : []
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

    DBus.SignalWatcher {
        busType: DBus.BusType.Session
        service: root.dbusService
        path: root.dbusPath
        iface: root.dbusInterface

        function dbusStateChanged(stateJson) {
            root.acceptState(stateJson, root.serviceEpoch, false);
        }
    }

    DBus.DBusServiceWatcher {
        id: serviceWatcher
        busType: DBus.BusType.Session
        watchedService: root.dbusService

        onRegisteredChanged: if (root.componentReady) root.handleServiceRegistration(registered)
    }

    // This timer only advances age labels; all state delivery is event-driven.
    Timer {
        interval: 5000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.nowSeconds = Date.now() / 1000
    }

    Component.onCompleted: {
        // Both watchers are complete now, so no update can land between the
        // initial method call and signal subscription.
        componentReady = true;
        if (serviceWatcher.registered) {
            handleServiceRegistration(true);
        }
    }

    function handleServiceRegistration(registered) {
        serviceEpoch += 1;
        acceptedInstance = "";
        acceptedRevision = -1;

        if (!registered) {
            reportStateError(i18n("The daemon's D-Bus service stopped. Check: systemctl --user status kdeclickd"));
            return;
        }

        lastError = i18n("Waiting for the daemon's initial D-Bus state.");
        requestState(serviceEpoch);
    }

    function requestState(epoch) {
        DBus.SessionBus.asyncCall({
            "service": dbusService,
            "path": dbusPath,
            "iface": dbusInterface,
            "member": "GetState"
        }, function (reply) {
            if (epoch === root.serviceEpoch) {
                root.acceptState(reply.value, epoch, true);
            }
        }, function (reply) {
            if (epoch === root.serviceEpoch && serviceWatcher.registered) {
                root.reportStateError(i18n("The daemon's GetState call failed: %1",
                    reply.error.message));
            }
        });
    }

    function acceptState(stateJson, epoch, authoritative) {
        if (epoch !== serviceEpoch || !serviceWatcher.registered) {
            return;
        }
        let state;
        try {
            if (stateJson === null || stateJson === undefined) {
                throw new TypeError("payload is empty");
            }
            state = JSON.parse(String(stateJson));
            if (!validState(state)) {
                throw new TypeError("payload does not match schema 1");
            }
        } catch (error) {
            reportStateError(i18n("The daemon returned unreadable state: %1", String(error)));
            return;
        }

        // GetState identifies the current bus owner authoritatively. This
        // prevents a queued signal from the previous owner winning a restart.
        if (authoritative && acceptedInstance !== state.instance) {
            acceptedRevision = -1;
        }
        if (authoritative || acceptedInstance.length === 0) {
            acceptedInstance = state.instance;
        }
        if (acceptedInstance !== state.instance) {
            return;
        }

        // A StateChanged signal can overtake the initial GetState response.
        if (state.revision <= acceptedRevision) {
            return;
        }

        acceptedRevision = state.revision;
        snapshot = state;
        lastError = "";
    }

    function validState(state) {
        const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
        const nonnegative = value => typeof value === "number" && isFinite(value) && value >= 0;
        if (!object(state) || state.schema !== 1 || typeof state.instance !== "string"
                || state.instance.length === 0 || !nonnegative(state.revision)
                || Math.floor(state.revision) !== state.revision
                || !nonnegative(state.updated) || state.updated === 0) {
            return false;
        }
        const activity = state.activity;
        const peak = state.peak;
        const net = state.network;
        const metrics = ["keystrokes", "click_left", "click_right", "click_middle",
            "click_side", "click_extra", "scroll_wheel", "scroll_touchpad",
            "motion_units", "network_rx_bytes", "network_tx_bytes"];
        return object(state.totals) && metrics.every(key => nonnegative(state.totals[key]))
            && Object.keys(state.totals).every(key => nonnegative(state.totals[key]))
            && object(activity) && Array.isArray(activity.values) && Array.isArray(activity.labels)
                && activity.values.length === 24 && activity.labels.length === 24
                && activity.values.every(nonnegative) && activity.labels.every(label => typeof label === "string")
            && (peak === null || (object(peak) && typeof peak.hour === "string" && nonnegative(peak.value)))
            && object(net) && ["initializing", "ok", "unavailable", "error"].indexOf(net.status) >= 0
                && typeof net.interface === "string" && typeof net.error === "string" && nonnegative(net.sampled_at)
                && (net.status !== "ok" || (net.interface.length > 0 && net.sampled_at > 0));
    }

    function reportStateError(message) {
        lastError = message;
        console.warn("Click Analytics: " + message);
    }

    function metric(key) {
        return hasSnapshot ? totals[key] : 0;
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

    function formatBytes(value) {
        const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
        let scaled = value;
        let unit = 0;
        while (scaled >= 1024 && unit < units.length - 1) {
            scaled /= 1024;
            ++unit;
        }
        const decimals = unit === 0 || scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
        return scaled.toLocaleString(Qt.locale(), 'f', decimals) + " " + units[unit];
    }
}
