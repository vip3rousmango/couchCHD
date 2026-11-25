import QtQuick

Item {
    id: tile
    required property var entry
    required property bool selected
    property real unit: 1
    signal activated()
    Accessible.role: entry.key === "system" ? Accessible.CheckBox : Accessible.Button
    Accessible.name: entry.label + ". " + entry.value + ". " + entry.detail
    Accessible.checked: entry.value === "On"
    Accessible.focused: selected
    Accessible.onPressAction: activated()
    HoverHandler { id: hover }
    TapHandler { onTapped: tile.activated() }
    Rectangle {
        anchors.fill: parent; anchors.margins: 3 * tile.unit; radius: 6 * tile.unit
        color: tile.selected ? "#d9ef9b" : hover.hovered ? "#37483e" : "#2c3932"
        Image {
            anchors.top: parent.top; anchors.left: parent.left; anchors.margins: 7 * tile.unit
            width: 54 * tile.unit; height: parent.height * .47; source: "../assets/" + tile.entry.icon
            fillMode: Image.PreserveAspectFit
        }
        Text {
            anchors.top: parent.top; anchors.right: parent.right; anchors.margins: 9 * tile.unit
            text: tile.entry.key === "inventory_system" ? "Inventory" : tile.entry.value
            font.pixelSize: 16 * tile.unit; color: tile.selected ? "#34482c" : "#c1ceb9"
        }
        Text {
            anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right
            anchors.leftMargin: 10 * tile.unit; anchors.rightMargin: 7 * tile.unit; anchors.bottomMargin: 7 * tile.unit
            text: tile.entry.label; textFormat: Text.PlainText; elide: Text.ElideRight
            font.pixelSize: 20 * tile.unit; font.weight: Font.DemiBold; color: tile.selected ? "#14221c" : "#eff1e9"
        }
    }
}
