#ifndef GUARDED_H
#define GUARDED_H

#if defined(CONFIG_SAMPLE) && CONFIG_VALUE > 1
int enabled();
#else
int disabled();
#endif

#ifdef UNKNOWN_MACRO
int unknown_branch();
#endif

#if 0
int compiled_out();
#endif

#if CONFIG_VALUE >= 2
int nested_enabled();
#endif

#if SOME_FEATURE(1)
int unknown_condition();
#endif

#endif
