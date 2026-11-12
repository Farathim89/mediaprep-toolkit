"""The Tcl side of a display scaling change (ui/dpi.py): the walk over every
window that scales its pixel options (::mpdpi::walk) and taking the pages
out of their canvases while everything is re-laid out (::mpdpi::detach /
attach). dpi._Manager.install evaluates _TCL once."""


# ---------------------------------------------------------------- Tcl walk
# Every pixel option of every window, scaled by new/old. Each value is kept
# with the scale it was taken at (::mpdpi::base), so 150 -> 300 -> 150 %
# gives back exactly the old values; a value the app set itself in between
# is taken as the new starting point. Values with units (2m, 1c, 10p) follow
# 'tk scaling' by themselves and are left alone.
_TCL = r"""
namespace eval ::mpdpi {
    variable base
    variable nbase
    array set base {}
    variable opts
    array set opts {
        Frame       {-width -height -padx -pady -borderwidth -highlightthickness}
        Toplevel    {-padx -pady -borderwidth -highlightthickness}
        Labelframe  {-width -height -padx -pady -borderwidth -highlightthickness}
        Label       {-padx -pady -borderwidth -highlightthickness -wraplength}
        Button      {-padx -pady -borderwidth -highlightthickness -wraplength}
        Checkbutton {-padx -pady -borderwidth -highlightthickness -wraplength}
        Radiobutton {-padx -pady -borderwidth -highlightthickness -wraplength}
        Message     {-width -padx -pady -borderwidth -highlightthickness}
        Canvas      {-width -height -borderwidth -highlightthickness -xscrollincrement
                     -yscrollincrement -insertwidth -selectborderwidth}
        Text        {-padx -pady -borderwidth -highlightthickness -spacing1 -spacing2
                     -spacing3 -insertwidth -selectborderwidth}
        Listbox     {-borderwidth -highlightthickness -selectborderwidth}
        Entry       {-borderwidth -highlightthickness -insertwidth -selectborderwidth}
        Spinbox     {-borderwidth -highlightthickness -insertwidth -selectborderwidth}
        Menu        {-borderwidth -activeborderwidth}
        Scale       {-width -length -sliderlength -borderwidth -highlightthickness}
        Scrollbar   {-width -borderwidth -highlightthickness}
        TFrame      {-width -height -padding -borderwidth}
        TLabelframe {-width -height -padding -borderwidth}
        TLabel      {-padding -wraplength}
        TButton     {-padding}
        TCheckbutton {-padding}
        TRadiobutton {-padding}
        TMenubutton {-padding}
        TNotebook   {-width -height -padding}
        TPanedwindow {-width -height}
        TProgressbar {-length}
        TScale      {-length}
        TEntry      {}
        TCombobox   {}
        TSpinbox    {}
        TScrollbar  {}
        TSeparator  {}
        Treeview    {}
    }
    variable n 0
}

# one value or a list of them, times r (plain pixel numbers only)
proc ::mpdpi::mul {v r} {
    set out {}
    foreach x $v {
        if {![string is double -strict $x]} { return -code error units }
        if {$x == 0} { lappend out 0; continue }
        set y [expr {round($x * $r)}]
        if {$y == 0} { set y [expr {$x > 0 ? 1 : -1}] }
        lappend out $y
    }
    return $out
}

# the new value for item `key` whose value is `cur` now
proc ::mpdpi::val {key cur old new} {
    variable base
    variable nbase
    if {[info exists base($key)] && [lindex $base($key) 2] eq $cur} {
        lassign $base($key) b bs
    } else {
        set b $cur
        set bs $old
    }
    set nv [mul $b [expr {double($new) / $bs}]]
    set nbase($key) [list $b $bs $nv]
    return $nv
}

proc ::mpdpi::walk {old new} {
    variable base
    variable nbase
    variable opts
    variable n 0
    array unset nbase
    array set nbase {}
    set queue [list .]
    while {[llength $queue]} {
        set next {}
        foreach w $queue {
            if {![winfo exists $w]} continue
            incr n
            set cls [winfo class $w]
            if {[info exists opts($cls)]} {
                set ol $opts($cls)
            } elseif {![catch {$w cget -padding}]} {
                set ol {-padding}
            } else {
                set ol {}
            }
            if {$cls in {Label Button Checkbutton Radiobutton}
                    && ![catch {$w cget -image} im] && $im ne ""} {
                lappend ol -width -height
            }
            foreach o $ol {
                if {[catch {$w cget $o} cur] || $cur eq ""} continue
                if {[catch {val "$w|$o" $cur $old $new} nv]} continue
                if {$nv ne $cur} { catch {$w configure $o $nv} }
            }
            set mgr [winfo manager $w]
            if {$mgr eq "pack" || $mgr eq "grid"} {
                if {![catch {$mgr info $w} info]} {
                    set ch {}
                    foreach o {-padx -pady -ipadx -ipady} {
                        if {![dict exists $info $o]} continue
                        set cur [dict get $info $o]
                        if {[catch {val "$w|$mgr$o" $cur $old $new} nv]} continue
                        if {$nv ne $cur} { lappend ch $o $nv }
                    }
                    if {[llength $ch]} { catch {$mgr configure $w {*}$ch} }
                }
            } elseif {$mgr eq "place"} {
                if {![catch {place info $w} info]} {
                    set ch {}
                    foreach o {-x -y -width -height} {
                        if {![dict exists $info $o]} continue
                        set cur [dict get $info $o]
                        if {$cur eq ""} continue
                        if {[catch {val "$w|place$o" $cur $old $new} nv]} continue
                        if {$nv ne $cur} { lappend ch $o $nv }
                    }
                    if {[llength $ch]} { catch {place configure $w {*}$ch} }
                }
            }
            # a grid master: its rows' / columns' minimum sizes and paddings
            if {![catch {grid size $w} gs]} {
                lassign $gs nc nr
                foreach {what cnt} [list columnconfigure $nc rowconfigure $nr] {
                    for {set i 0} {$i < $cnt} {incr i} {
                        foreach o {-minsize -pad} {
                            if {[catch {grid $what $w $i $o} cur] || $cur == 0} continue
                            if {[catch {val "$w|$what$i$o" $cur $old $new} nv]} continue
                            if {$nv ne $cur} { catch {grid $what $w $i $o $nv} }
                        }
                    }
                }
            }
            if {$cls eq "Text"} {
                foreach t [$w tag names] {
                    foreach o {-spacing1 -spacing2 -spacing3 -lmargin1 -lmargin2 -rmargin -offset} {
                        if {[catch {$w tag cget $t $o} cur] || $cur eq ""} continue
                        if {[catch {val "$w|tag$t$o" $cur $old $new} nv]} continue
                        if {$nv ne $cur} { catch {$w tag configure $t $o $nv} }
                    }
                }
            } elseif {$cls eq "Treeview"} {
                set cols [list #0]
                catch {lappend cols {*}[$w cget -columns]}
                foreach c $cols {
                    foreach o {-width -minwidth} {
                        if {[catch {$w column $c $o} cur] || $cur eq ""} continue
                        if {[catch {val "$w|col$c$o" $cur $old $new} nv]} continue
                        if {$nv ne $cur} { catch {$w column $c $o $nv} }
                    }
                }
            }
            if {[winfo toplevel $w] eq $w && $cls ne "Menu"} {
                if {![catch {wm minsize $w} cur] && ([lindex $cur 0] > 1 || [lindex $cur 1] > 1)} {
                    if {![catch {val "$w|minsize" $cur $old $new} nv] && $nv ne $cur} {
                        catch {wm minsize $w {*}$nv}
                    }
                }
            }
            lappend next {*}[winfo children $w]
        }
        set queue $next
    }
    array unset base
    array set base [array get nbase]
    array unset nbase
    return $n
}

# Windows embedded in canvases (the pages of the page view, the help
# cards of the Info page): taken out of their canvas while everything is
# re-laid out and put back afterwards - re-laying out a big frame while a
# canvas manages it is ~5-10x slower (each size request of the frame goes
# through the canvas at once). all=0: only the ones not on screen.
proc ::mpdpi::detach {all} {
    set out {}
    set queue [list .]
    while {[llength $queue]} {
        set next {}
        foreach w $queue {
            if {[winfo class $w] eq "Canvas"} {
                set vis [winfo viewable $w]
                foreach i [$w find all] {
                    if {[$w type $i] ne "window"} continue
                    set win [$w itemcget $i -window]
                    if {$win eq ""} continue
                    if {!$all && $vis && [$w itemcget $i -state] ne "hidden"} continue
                    lappend out [list $w $i $win]
                    $w itemconfigure $i -window {}
                }
            }
            lappend next {*}[winfo children $w]
        }
        set queue $next
    }
    return $out
}

proc ::mpdpi::attach {items} {
    foreach it $items {
        lassign $it c i win
        if {[winfo exists $c] && [winfo exists $win]} {
            catch {$c itemconfigure $i -window $win}
        }
    }
}
"""
