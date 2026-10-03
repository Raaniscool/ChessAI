// Calculation arrows (Lichess-style): hold the right mouse button on a square, drag to another,
// release -> an arrow. Right-click a single square -> a circle. Drawing the same arrow again
// removes it; a left click on the board, or the position changing, clears them all.
// Modifiers pick the colour as on Lichess: Shift = red, Alt = blue, Shift+Alt = orange.
//
// Purely visual: built on cm-chessboard's RightClickAnnotator, which only listens to the
// right button. cm-chessboard's move input only reacts to the left button, so a right-drag can
// never move a piece, and nothing here touches the game, the history, the engine or the AI.
// `isDisabled()` turns drawing off (while the AI is moving pieces by itself).

import {EXTENSION_POINT} from "./vendor/cm-chessboard/src/model/Extension.js"
import {RightClickAnnotator} from "./vendor/cm-chessboard/src/extensions/right-click-annotator/RightClickAnnotator.js"

export class CalcArrows extends RightClickAnnotator {
  constructor(chessboard, props = {}) {
    super(chessboard, props)
    this.isDisabled = props.isDisabled || (() => false)
    this.onLeftDown = event => {
      if (event.button === 0) this.clear()
    }
    this.chessboard.context.addEventListener("mousedown", this.onLeftDown)
    // a move was made or another position is shown: the old markings no longer apply
    this.registerExtensionPoint(EXTENSION_POINT.positionChanged, () => this.clear())
    this.registerExtensionPoint(EXTENSION_POINT.destroy, () => {
      this.chessboard.context.removeEventListener("mousedown", this.onLeftDown)
    })
    this.chessboard.clearCalculation = () => this.clear()
  }

  clear() {
    this.dragStart = undefined
    this.removePreviewArrow()
    this.removeOwnArrows()
    this.removeOwnMarkers()
  }

  onMouseDown(event) {
    if (event.button === 2 && this.isDisabled()) {
      this.dragStart = undefined
      return
    }
    super.onMouseDown(event)
  }
}
