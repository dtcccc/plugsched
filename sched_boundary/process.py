from yaml import load, dump, resolver, CLoader as Loader, CDumper as Dumper
from itertools import islice as skipline
from itertools import chain as _chain
from sh import readelf
import logging
import json
import os
import copy
chain = _chain.from_iterable

fn_symbol_classify = {}
config = None
# store sympos for local functions in module files
local_sympos = {}

# Use set as the default sequencer for yaml
Loader.add_constructor(resolver.BaseResolver.DEFAULT_SEQUENCE_TAG,
                       lambda loader, node: set(loader.construct_sequence(node)))
Dumper.add_representer(set, lambda dumper, node: dumper.represent_list(node))
Dumper.add_representer(unicode,
                       lambda dumper, data: dumper.represent_scalar(u'tag:yaml.org,2002:str', data))

def read_config():
    with open('sched_boundary.yaml') as f:
        return load(f, Loader)

def all_meta_files():
    for r, dirs, files in os.walk('.'):
        for file in files:
            if file.endswith('.sched_boundary'):
                yield os.path.join(r, file)

def read_meta(filename):
    with open(filename) as f:
        return json.load(f)

# This method connects gcc-plugin with vmlinux (or the ld linker)
# It serves two purposes right now:
#   1. find functions in vmlinux, to calc optimized_out later
#   2. find sympos from vmlinux, which will be used to check confliction with kpatch
# This must be called after we have read all files, and all vagueness has been solved.
#
# Four pitfalls because of disagreement between vmlinux and gcc-plugin, illustrated with examples
#
# Disagreement 1: vmlinux thinks XXX is in core.c, plugsched thinks it's in kernel/sched/core.c
# Disagreement 2: vmlinux thinks XXX is in core.c, plugsched thinks it's in sched.h
# Disagreement 3: vmlinux thinks XXX is in usercopy_64.c, plugsched thinks it's in core.c
# Disagrement: 4: vmlinux optimizes XXX to XXX.isra.1, plugsched remains XXX.

def get_in_any(key, files):
    for file in files:
        if (key, file) in fn_symbol_classify['fn']:
            break
    return file

def find_in_vmlinux():
    in_vmlinux = set()
    fn_pos = {}
    for line in skipline(readelf('vmlinux', syms=True, wide=True, _iter=True), 3, None):
        fields = line.split()
        if len(fields) != 8: continue
        symtype, scope, key = fields[3], fields[4], fields[7]

        if symtype == 'FILE':
            filename = key
            # Disagreement 1:
            if filename in config['mod_files_basename']:
                filename = config['mod_files_basename'][filename]
            continue
        elif symtype != 'FUNC':
            continue

        file = filename
        # Disagreement 4
        if '.' in key: key = key[:key.index('.')]

        if scope == 'LOCAL':
            fn_pos[key] = fn_pos.get(key, 0) + 1
            if filename not in config['mod_files']: continue

            # Disagreement 2
            if (key, filename) not in fn_symbol_classify['fn']:
                file = get_in_any(key, config['mod_header_files'])

            local_sympos[(key, file)] = fn_pos[key]
        else:
            # Disagreement 3
            file = get_in_any(key, config['mod_files'])

        if file in config['mod_files']: in_vmlinux.add((key, file))

    return in_vmlinux

# __insiders is a global variable only used by these two functions
__insiders = None

def inflect_one(edge):
    to_sym = tuple(edge['to'])
    if to_sym in __insiders:
        from_sym = tuple(edge['from'])
        if from_sym not in __insiders and \
           from_sym not in fn_symbol_classify['interface'] and \
           from_sym not in fn_symbol_classify['fn_ptr'] and \
           from_sym not in fn_symbol_classify['init']:
            return to_sym
    return None

def inflect(initial_insiders, edges):
    global __insiders
    __insiders = copy.deepcopy(initial_insiders)
    while True:
        delete_insider = filter(None, map(inflect_one, edges))
        if not delete_insider:
            break
        __insiders -= set(delete_insider)
    return __insiders

if __name__ == '__main__':
    # Read all files generated by SchedBoundaryCollect, and export_jump.h, and sched_boundary.yaml
    config = read_config()
    config['mod_files_basename'] = {os.path.basename(f): f for f in config['mod_files']}
    config['mod_header_files'] = [f for f in config['mod_files'] if f.endswith('.h')]
    metas = map(read_meta, all_meta_files())
    fn_symbol_classify['fn'] = set()
    fn_symbol_classify['init'] = set()
    fn_symbol_classify['interface'] = set()
    fn_symbol_classify['fn_ptr'] = set()
    fn_symbol_classify['mod_fns'] = set()
    global_fn_dict = {}
    edges= []

    # first pass: calc init and interface set
    for meta in metas:
        for fn in meta['fn']:
            fn_sign  = tuple(fn['signature'])
            fn_symbol_classify['fn'].add(fn_sign)

            if fn_sign[1] in config['mod_files']: fn_symbol_classify['mod_fns'].add(fn_sign)
            if fn['init']: fn_symbol_classify['init'].add(fn_sign)
            if fn['public']: global_fn_dict[fn['name']] = fn['file']

        for fn in meta['interface']:
            fn_symbol_classify['interface'].add(tuple(fn))

    # second pass: fix vague filename, calc fn_ptr and edge set
    for meta in metas:
        for fn in meta['fn_ptr']:
            if fn[1] == '?': fn[1] = global_fn_dict[fn[0]]
            if fn[1] in config['mod_files'] and tuple(fn) not in fn_symbol_classify['interface']:
                fn_symbol_classify['fn_ptr'].add(tuple(fn))

        for edge in meta['edge']:
            if edge['to'][1] == '?':
                if edge['to'][0] not in global_fn_dict:
                    # bypass gcc built-in funtion
                    continue
                else:
                    edge['to'][1] = global_fn_dict[edge['to'][0]]
            edges.append(edge)

    fn_symbol_classify['initial_insider'] = fn_symbol_classify['mod_fns'] - fn_symbol_classify['interface'] - fn_symbol_classify['fn_ptr']
    fn_symbol_classify['in_vmlinux'] = find_in_vmlinux()

    # Inflect outsider functions
    fn_symbol_classify['insider'] = inflect(fn_symbol_classify['initial_insider'], edges)
    fn_symbol_classify['outsider'] = fn_symbol_classify['initial_insider'] - fn_symbol_classify['insider']
    fn_symbol_classify['optimized_out'] = fn_symbol_classify['outsider'] - fn_symbol_classify['in_vmlinux']
    fn_symbol_classify['tainted'] = (fn_symbol_classify['interface'] | fn_symbol_classify['fn_ptr'] | fn_symbol_classify['insider']) & fn_symbol_classify['in_vmlinux']

    for output_item in ['outsider', 'fn_ptr', 'interface', 'init', 'insider', 'optimized_out']:
        config['function'][output_item] = [fn for fn in fn_symbol_classify[output_item]]

#    # Handle Struct public fields. The right hand side gives an example
#    struct_properties = {
#        struct: {                                                                          # cfs_rq:
#            'public_fields': set(chain(                                                         #   public_fields:
#                [field for field, users in m['struct'][struct]['public_fields'].iteritems()     #   - nr_uninterruptible
#                       if any(user['file'] not in config['mod_files'] for user in users)        #   # ca_uninterruptible (in cpuacct.c) referenced it.
#                       or set(map(Symbol.get, users)) & fn_symbol_classify['outsider']]         #   # maybe some outsider (in scheduler c files) referenced it.
#                for m in metas                                                                  ## for all files output by SchedBoundaryCollect
#                if struct in m['struct']                                                        ## and only if this file has structure information
#             )),
#            'all_fields': set(chain(
#                m['struct'][struct]['all_fields']
#                for m in metas
#                if struct in m['struct']
#            ))
#        }
#        for struct in set(chain(m['struct'].keys() for m in metas))
#    }
#
#    with open('sched_boundary_doc.yaml', 'w') as f:
#        dump(struct_properties, f, Dumper)
    with open('sched_boundary_extract.yaml', 'w') as f:
        dump(config, f, Dumper)
    with open('tainted_functions', 'w') as f:
        f.write('\n'.join(["{fn} {sympos}".format(fn=fn[0], sympos=local_sympos.get(fn, 0)) for fn in fn_symbol_classify['tainted']]))
    with open('interface_fn_ptrs', 'w') as f:
        f.write('\n'.join([fn[0] for fn in config['function']['interface']]))
        f.write('\n')
        f.write('\n'.join(['__mod_' + fn[0] for fn in config['function']['fn_ptr']]))
