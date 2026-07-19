import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    // Plain property rather than an alias to currentValue: the config system
    // populates cfg_* before onCompleted, so the ComboBox can restore its index
    // from it. An alias would read back its own uninitialised default instead.
    property string cfg_panelWindow
    property alias cfg_mouseDpi: mouseDpi.value

    QQC2.ComboBox {
        id: panelWindow
        Kirigami.FormData.label: i18n("Panel numbers show:")
        textRole: "label"
        valueRole: "key"
        model: [
            {"key": "all", "label": i18n("All time")},
            {"key": "today", "label": i18n("Today")},
            {"key": "week", "label": i18n("This week")}
        ]
        onActivated: cfg_panelWindow = currentValue
        Component.onCompleted: currentIndex = Math.max(0, indexOfValue(cfg_panelWindow))
    }

    QQC2.SpinBox {
        id: mouseDpi
        Kirigami.FormData.label: i18n("Mouse DPI:")
        from: 100
        to: 32000
        stepSize: 100
    }

    QQC2.Label {
        text: i18n("Pointer travel is converted to distance using this DPI.\nOnly the travel figure depends on it; counts do not.")
        opacity: 0.7
        wrapMode: Text.WordWrap
        Layout.maximumWidth: Kirigami.Units.gridUnit * 18
    }
}
