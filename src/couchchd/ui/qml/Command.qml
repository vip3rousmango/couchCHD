import QtQuick
import QtQuick.Layouts

Rectangle {
    id: button
    required property string label
    property string glyph: ""
    property bool filled: false
    property real unit: 1
    property bool compact: false
    signal activated()
    implicitWidth: group.implicitWidth + (filled ? 28 : 12) * unit
    implicitHeight: (compact ? 37 : 48) * unit
    radius: 5 * unit
    opacity: enabled ? 1.0 : 0.48
    color: filled ? (hover.hovered ? "#ebffaf" : "#dbf483") : hover.hovered ? "#34413a" : "transparent"
    Accessible.role: Accessible.Button
    Accessible.name: glyph + " " + label
    Accessible.onPressAction: activated()
    HoverHandler { id: hover }
    TapHandler { onTapped: button.activated() }
    RowLayout {
        id: group; anchors.centerIn: parent; spacing: 7 * button.unit
        Rectangle {
            visible: button.glyph.length > 0
            implicitWidth: Math.max(27 * button.unit, badge.implicitWidth + 8 * button.unit)
            implicitHeight: 27 * button.unit; radius: 6 * button.unit
            color: button.filled ? "#223127" : "#435247"
            Text {
                id: badge; anchors.centerIn: parent; text: button.glyph; textFormat: Text.PlainText
                font.pixelSize: 17 * button.unit; font.bold: true
                color: button.filled ? "#e9fabc" : "#f0f4e9"
            }
        }
        Text {
            text: button.label; textFormat: Text.PlainText
            font.pixelSize: (button.compact ? 19 : 23) * button.unit; font.weight: Font.DemiBold
            color: button.filled ? "#202e22" : "#eff1e9"
        }
    }
}
