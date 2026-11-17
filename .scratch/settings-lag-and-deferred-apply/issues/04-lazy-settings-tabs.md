# 04: Lazy Settings tab construction & caching

**What to build:** Eliminate the first-open and every-open widget-construction burst by building only the initially visible Settings tab when the dialog first opens, building each remaining tab lazily on demand when switched to, and preserving built tab widgets across Open/Apply cycles unless the UI language actually changes.

**Blocked by:** 02: Draft settings lifecycle, dirty Apply button & diffed commit

**Status:** resolved

- [x] On first open, only the active Settings tab's inner widgets are built; unselected tabs remain empty until selected.
- [x] Switching to an unbuilt tab constructs its widgets immediately and populates them with current draft values.
- [x] Re-opening Settings or clicking **Apply** without changing Language does not destroy or recreate already-built tab widgets.
- [x] When Language actually changes on **Apply**, only tabs that were already constructed are rebuilt in the new language (unvisited tabs build in the new language when first visited).
