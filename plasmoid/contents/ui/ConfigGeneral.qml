import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    property alias cfg_mouseDpi: mouseDpi.value
    property alias cfg_touchpadPerNotch: touchpadPerNotch.value

    QQC2.SpinBox {
        id: mouseDpi
        Kirigami.FormData.label: i18n("Mouse DPI:")
        from: 100
        to: 32000
        stepSize: 100
    }

    QQC2.SpinBox {
        id: touchpadPerNotch
        Kirigami.FormData.label: i18n("Touchpad events per notch:")
        from: 1
        to: 500
    }

    QQC2.Label {
        text: i18n("Pointer travel is converted to distance using this DPI.\nOnly the travel figure depends on it; counts do not.")
        opacity: 0.7
        wrapMode: Text.WordWrap
        Layout.maximumWidth: Kirigami.Units.gridUnit * 18
    }
}
