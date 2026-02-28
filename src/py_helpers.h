/*
 * Minimal C helpers for Python and greenlet C API operations that
 * Zig's @cImport cannot translate (macro-based constants, function-pointer
 * tables, inline functions, etc.).
 *
 * This file is compiled as C and linked into the Zig extension module.
 */
#ifndef PY_HELPERS_H
#define PY_HELPERS_H

#include <Python.h>

/* --- Python constants that are macros --- */
PyObject *py_helper_none(void);
void py_helper_incref(PyObject *o);
void py_helper_decref(PyObject *o);
int py_helper_meth_noargs(void);
int py_helper_meth_varargs(void);

/* --- Module creation wrappers --- */
PyMethodDef py_helper_method_sentinel(void);
PyObject *py_helper_module_create(PyModuleDef *def);
PyModuleDef_Base py_helper_module_def_head_init(void);

/* --- Greenlet C API wrappers --- */

/* Initialize the greenlet C API function-pointer table.
 * Must be called once during module init before any other greenlet call.
 * Returns 0 on success, -1 on failure (with Python exception set). */
int py_helper_greenlet_import(void);

/* greenlet.getcurrent() — returns a NEW reference. */
PyObject *py_helper_greenlet_getcurrent(void);

/* greenlet.greenlet(run, parent) — returns a NEW reference.
 * parent may be NULL (defaults to current greenlet). */
PyObject *py_helper_greenlet_new(PyObject *run, PyObject *parent);

/* g.switch(*args, **kwargs) — returns a NEW reference.
 * args and kwargs may be NULL. */
PyObject *py_helper_greenlet_switch(PyObject *greenlet, PyObject *args, PyObject *kwargs);

/* Query greenlet state. */
int py_helper_greenlet_active(PyObject *greenlet);
int py_helper_greenlet_started(PyObject *greenlet);
int py_helper_greenlet_main(PyObject *greenlet);

/* Tuple helpers */
PyObject *py_helper_tuple_new(Py_ssize_t size);
int py_helper_tuple_setitem(PyObject *tuple, Py_ssize_t pos, PyObject *item);
PyObject *py_helper_tuple_getitem(PyObject *tuple, Py_ssize_t pos);

/* Error handling */
PyObject *py_helper_err_occurred(void);
void py_helper_err_print(void);
void py_helper_err_set_string(PyObject *type, const char *message);
PyObject *py_helper_exc_oserror(void);
PyObject *py_helper_exc_runtime_error(void);
PyObject *py_helper_err_no_memory(void);
void py_helper_err_set_from_errno(int err);

/* Bytes helpers */
PyObject *py_helper_bytes_from_string_and_size(const char *v, Py_ssize_t len);
char *py_helper_bytes_as_string(PyObject *bytes);
Py_ssize_t py_helper_bytes_get_size(PyObject *bytes);

/* Long helpers */
long py_helper_long_as_long(PyObject *obj);

/* Callable helpers */
PyObject *py_helper_call_no_args(PyObject *callable);

/* Boolean helpers (Py_True / Py_False are macros) */
PyObject *py_helper_true(void);
PyObject *py_helper_false(void);

/* Additional exception type accessors */
PyObject *py_helper_exc_value_error(void);

/* GIL management (for releasing GIL during blocking I/O) */
typedef struct { void *state; } PyHelperThreadState;
PyHelperThreadState py_helper_save_thread(void);
void py_helper_restore_thread(PyHelperThreadState state);

#endif /* PY_HELPERS_H */
