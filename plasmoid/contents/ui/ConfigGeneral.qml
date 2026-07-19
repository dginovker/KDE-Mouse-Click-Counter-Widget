import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    property alias cfg_mouseDpi: mouseDpi.value

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
