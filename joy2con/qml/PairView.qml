// SPDX-License-Identifier: GPL-3.0-or-later
// Both Joy-Cons side by side, live, with each one's battery underneath.
// Clicking a Joy-Con opens its page.
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

RowLayout {
    id: pair
    property var joycons: ({})
    property var known: ({})
    property var ledsFor: (side) => 0
    property string highlightSide: ""   // set while that Joy-Con's row elsewhere is hovered
    signal open(string side)
    spacing: Kirigami.Units.gridUnit

    Repeater {
        model: ["left", "right"]
        delegate: ColumnLayout {
            id: half
            required property string modelData
            readonly property var jc: pair.joycons[modelData]
            readonly property var k: pair.known[modelData]
            readonly property bool live: !!jc
            readonly property var colors: live ? (jc.info.colors || {}) : ((k && k.colors) || {})
            readonly property int pct: live ? jc.battery_pct : 0
            readonly property bool charging: live && jc.charge > 0 && jc.charge !== 0x20
            readonly property bool hot: hover.hovered || pair.highlightSide === modelData
            spacing: Kirigami.Units.largeSpacing

            JoyConArt {
                id: drawing
                Layout.preferredWidth: Kirigami.Units.gridUnit * 7.5
                Layout.preferredHeight: Layout.preferredWidth * 440 / 160
                side: half.modelData
                accent: half.colors.highlight || (half.modelData === "left" ? "#9be1e6" : "#ff8c5f")
                buttonsDown: half.live ? half.jc.buttons : []
                stick: half.live ? half.jc.stick : [0, 0]
                playerLeds: pair.ledsFor(half.modelData)
                live: half.live
                highlighted: half.hot
                opacity: half.live || half.hot ? 1 : 0.35
                scale: half.hot ? 1.04 : 1
                Behavior on opacity { NumberAnimation { duration: Kirigami.Units.shortDuration } }
                Behavior on scale { NumberAnimation { duration: Kirigami.Units.shortDuration; easing.type: Easing.OutCubic } }
                HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
                TapHandler { onTapped: pair.open(half.modelData) }

                Rectangle {   // highlight plate behind the drawing
                    z: -1
                    anchors.fill: parent
                    anchors.margins: -Kirigami.Units.largeSpacing
                    radius: Kirigami.Units.cornerRadius * 2
                    color: Kirigami.Theme.hoverColor
                    opacity: half.hot ? 0.16 : 0
                    Behavior on opacity { NumberAnimation { duration: Kirigami.Units.shortDuration } }
                }
            }
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: Kirigami.Units.smallSpacing
                Kirigami.Icon {
                    visible: half.live
                    implicitWidth: Kirigami.Units.iconSizes.small
                    implicitHeight: Kirigami.Units.iconSizes.small
                    source: "battery-" + String(Math.round(half.pct / 10) * 10).padStart(3, "0") + (half.charging ? "-charging" : "")
                }
                QQC2.Label {
                    text: half.live ? half.pct + "%" : (half.k ? "Asleep" : "Not paired")
                    color: half.live && half.pct < 15 && !half.charging ? Kirigami.Theme.negativeTextColor
                         : half.live ? Kirigami.Theme.textColor : Kirigami.Theme.disabledTextColor
                    font.features: { "tnum": 1 }
                }
            }
        }
    }
}
