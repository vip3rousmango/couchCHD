import QtQuick
import QtQuick.Layouts

Rectangle {
    id: cell
    required property var entry
    required property bool selected
    property bool expanded: false
    property real unit: 1
    signal activated()
    color: selected ? "#d9ef9b" : hover.hovered ? "#303e39" : "transparent"
    radius: 6 * unit
    implicitHeight: Math.max(56 * unit, line.implicitHeight + 20 * unit)
    Accessible.role: Accessible.Button
    Accessible.name: entry.label + ". " + entry.detail + ". " + entry.value
    Accessible.focused: selected
    Accessible.onPressAction: activated()
    HoverHandler { id: hover }
    TapHandler { onTapped: cell.activated() }
    RowLayout {
        id: line
        anchors.left: parent.left; anchors.right: parent.right
        anchors.leftMargin: 18 * cell.unit; anchors.rightMargin: 18 * cell.unit
        anchors.verticalCenter: parent.verticalCenter
        spacing: 20 * cell.unit
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2 * cell.unit
            Text {
                Layout.fillWidth: true
                text: cell.entry.label; textFormat: Text.PlainText
                color: cell.selected ? "#14221c" : "#eff1e9"
                font.pixelSize: 25 * cell.unit; font.weight: Font.DemiBold
                wrapMode: cell.expanded ? Text.Wrap : Text.NoWrap
                elide: cell.expanded ? Text.ElideNone : Text.ElideRight
            }
            Text {
                Layout.fillWidth: true
                visible: text.length > 0
                text: cell.entry.detail; textFormat: Text.PlainText
                color: cell.selected ? "#34482c" : "#b4c2b7"
                font.pixelSize: 24 * cell.unit
                wrapMode: cell.expanded ? Text.Wrap : Text.NoWrap
                elide: cell.expanded ? Text.ElideNone : Text.ElideMiddle
            }
        }
        Text {
            visible: text.length > 0
            Layout.maximumWidth: cell.width * 0.29
            text: cell.entry.value; textFormat: Text.PlainText
            color: cell.selected ? "#1f3523" : "#ccddad"
            font.pixelSize: 21 * cell.unit; font.weight: Font.DemiBold
            elide: Text.ElideRight
        }
    }
    Rectangle {
        visible: !cell.selected
        anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right
        anchors.leftMargin: 18 * cell.unit; anchors.rightMargin: 18 * cell.unit
        height: 1; color: "#39463f"
    }
}
