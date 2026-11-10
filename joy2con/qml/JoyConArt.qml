// SPDX-License-Identifier: GPL-3.0-or-later
// The live Joy-Con drawing: line art from art.py in the desktop theme's
// colours, the controller's accent colour on pressed buttons, the stick cap
// following the stick, and the player lights the host has set.
import QtQuick
import org.kde.kirigami as Kirigami

Item {
    id: art
    required property string side
    property color accent: side === "left" ? "#9be1e6" : "#ff8c5f"
    property var buttonsDown: []
    property var stick: [0, 0]
    property int playerLeds: 0
    property bool live: true
    property bool highlighted: false   // traces the outline in the accent colour
    readonly property real s: width / 160
    readonly property color ink: Kirigami.Theme.textColor
    readonly property color fillColor: Qt.tint(Kirigami.Theme.backgroundColor, Qt.rgba(ink.r, ink.g, ink.b, 0.06))
    readonly property color lineColor: Qt.tint(Kirigami.Theme.backgroundColor, Qt.rgba(ink.r, ink.g, ink.b, 0.75))
    readonly property real stroke: 1.7 * s

    function hex(c) { return c.toString().slice(1); }
    function pressed(name) { return buttonsDown.indexOf(name) >= 0; }

    implicitWidth: 160
    implicitHeight: 440

    Image {
        anchors.fill: parent
        source: "image://joycon/" + art.side + "/fill/" + art.hex(art.fillColor)
        sourceSize: Qt.size(Math.ceil(width * Screen.devicePixelRatio), Math.ceil(height * Screen.devicePixelRatio))
    }
    Repeater {   // pressed buttons fill with the accent colour, under the outlines
        model: backend.layout(art.side)
        delegate: Rectangle {
            required property var modelData
            readonly property bool round: modelData.shape === "c"
            width: (round ? modelData.a * 2 : modelData.a) * art.s
            height: (round ? modelData.a * 2 : modelData.b) * art.s
            x: modelData.cx * art.s - width / 2
            y: modelData.cy * art.s - height / 2
            radius: round ? width / 2 : Math.min(width, height) * 0.25
            color: art.accent
            opacity: art.pressed(modelData.name) ? 0.7 : 0
            Behavior on opacity { NumberAnimation { duration: 40 } }
        }
    }
    Repeater {   // shoulder buttons light up in their own shape
        model: backend.shoulders(art.side)
        delegate: Image {
            required property string modelData
            anchors.fill: parent
            source: "image://joycon/" + art.side + "/shoulder/" + modelData + "/" + art.hex(art.accent)
            sourceSize: Qt.size(Math.ceil(width * Screen.devicePixelRatio), Math.ceil(height * Screen.devicePixelRatio))
            opacity: art.pressed(modelData) ? 0.7 : 0
            Behavior on opacity { NumberAnimation { duration: 40 } }
        }
    }
    Image {
        anchors.fill: parent
        source: "image://joycon/" + art.side + "/lines/" + art.hex(art.lineColor) + "/" + art.hex(art.accent)
        sourceSize: Qt.size(Math.ceil(width * Screen.devicePixelRatio), Math.ceil(height * Screen.devicePixelRatio))
    }
    Image {   // hover outline
        anchors.fill: parent
        source: "image://joycon/" + art.side + "/outline/" + art.hex(art.accent)
        sourceSize: Qt.size(Math.ceil(width * Screen.devicePixelRatio), Math.ceil(height * Screen.devicePixelRatio))
        opacity: art.highlighted ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Kirigami.Units.shortDuration } }
    }
    Rectangle {   // stick cap
        readonly property var c: backend.stick(art.side)
        readonly property bool click: art.pressed(art.side === "left" ? "LSTICK" : "RSTICK")
        width: c[3] * 2 * art.s
        height: width
        radius: width / 2
        x: (c[0] + Math.max(-1.2, Math.min(1.2, art.stick[0])) * c[2]) * art.s - width / 2
        y: (c[1] - Math.max(-1.2, Math.min(1.2, art.stick[1])) * c[2]) * art.s - height / 2
        color: click ? Qt.tint(art.fillColor, Qt.rgba(art.accent.r, art.accent.g, art.accent.b, 0.7)) : art.fillColor
        border.color: art.lineColor
        border.width: art.stroke
        Rectangle {
            anchors.centerIn: parent
            width: parent.width * 0.58; height: width; radius: width / 2
            color: "transparent"
            border.color: art.lineColor
            border.width: art.stroke * 0.8
            opacity: 0.5
        }
    }
    Repeater {   // player lights
        model: 4
        delegate: Rectangle {
            id: led
            required property int index
            readonly property var l: backend.leds(art.side)
            readonly property bool flashing: ((art.playerLeds >> 4) >> index) & 1
            readonly property bool on: art.live && (((art.playerLeds >> index) & 1) || flashing)
            width: 4 * art.s; height: 6 * art.s; radius: art.s
            x: l[0] * art.s - width / 2
            y: l[1][index] * art.s - height / 2
            color: on ? "#7dffa0" : "transparent"
            SequentialAnimation on opacity {
                running: led.flashing
                loops: Animation.Infinite
                NumberAnimation { to: 0.15; duration: 300 }
                NumberAnimation { to: 1; duration: 300 }
            }
        }
    }
}
