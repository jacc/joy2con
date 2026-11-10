// SPDX-License-Identifier: GPL-3.0-or-later
// Rolling plot of one Joy-Con's rotation rate (degrees per second) about its
// three axes, with its current orientation underneath.
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

ColumnLayout {
    id: g
    property var jc
    property var samples: []
    readonly property int count: 240
    readonly property var axes: [
        { name: "Pitch", color: "#e5484d" },
        { name: "Roll", color: "#30a46c" },
        { name: "Yaw", color: "#3e7bfa" },
    ]
    spacing: Kirigami.Units.smallSpacing

    onJcChanged: {
        if (!jc) return;
        samples.push(jc.gyro);
        if (samples.length > count) samples.shift();
        canvas.requestPaint();
    }

    Canvas {
        id: canvas
        Layout.fillWidth: true
        Layout.preferredHeight: Kirigami.Units.gridUnit * 6
        onPaint: {
            const ctx = getContext("2d");
            const ink = Kirigami.Theme.textColor;
            ctx.reset();
            ctx.strokeStyle = Qt.rgba(ink.r, ink.g, ink.b, 0.15);
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(0, height / 2); ctx.lineTo(width, height / 2); ctx.stroke();
            const s = g.samples;
            let peak = 90;
            for (const v of s) for (const c of v) peak = Math.max(peak, Math.abs(c));
            const k = (height / 2 - 2) / peak;
            for (let a = 0; a < 3; a++) {
                ctx.strokeStyle = g.axes[a].color;
                ctx.lineWidth = 1.5;
                ctx.beginPath();
                for (let i = 0; i < s.length; i++) {
                    const x = i / (g.count - 1) * width, y = height / 2 - s[i][a] * k;
                    if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
                }
                ctx.stroke();
            }
            ctx.fillStyle = Kirigami.Theme.disabledTextColor;
            ctx.font = Kirigami.Theme.smallFont.pixelSize + "px sans-serif";
            ctx.fillText(peak.toFixed(0) + "°/s", 2, Kirigami.Theme.smallFont.pixelSize);
        }
    }
    RowLayout {
        spacing: Kirigami.Units.gridUnit
        Repeater {
            model: g.axes
            delegate: RowLayout {
                required property int index
                required property var modelData
                spacing: Kirigami.Units.smallSpacing
                Rectangle { implicitWidth: Kirigami.Units.gridUnit * 0.8; implicitHeight: 2; color: modelData.color }
                QQC2.Label {
                    text: modelData.name + (g.jc ? " " + g.jc.angles[index].toFixed(0) + "°" : "")
                    font.features: { "tnum": 1 }
                }
            }
        }
    }
}
