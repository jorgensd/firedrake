#include <stdio.h>
#include <stdlib.h>
#include <spatialindex/capi/sidx_api.h>
#include <libsupermesh-c.h>
#include <float.h>
#include <evaluate.h>

int locate_cell(struct Function *f,
        double *x,
        int dim,
        ref_cell_l1_dist try_candidate,
        ref_cell_l1_dist_xtr try_candidate_xtr,
        void *temp_ref_coords,
        void *found_ref_coords,
        double *found_ref_cell_dist_l1)
{
    RTError err = RT_None;
    int cell = -1;
    /* NOTE: temp_ref_coords and found_ref_coords are actually of type
    struct ReferenceCoords but can't be declared as such in the function
    signature because the dimensions of the reference coordinates in the
    ReferenceCoords struct are defined by python when the code which
    surrounds this is declared in pointquery_utils.py. We cast when we use the
    ref_coords_copy function and trust that the underlying memory which the
    pointers refer to is updated as necessary. */
    double ref_cell_dist_l1 = DBL_MAX;
    double current_ref_cell_dist_l1 =  -0.5;
    /* NOTE: `tolerance`, which is used throughout this funciton, is a static
       variable defined outside this function when putting together all the C
       code that needs to be compiled - see pointquery_utils.py */

    if (f->sidx2) {
        // int64_t *ids = NULL;
        // uint64_t nids = 0;
        int *ids = NULL;
        int nids = 0;
        /* We treat our list of candidate cells (ids) from libspatialindex's
            Index_Intersects_id as our source of truth: the point must be in
            one of the cells. */
        // err = Index_Intersects_id(f->sidx, x, x, dim, &ids, &nids);
        libsupermesh_query_rtree(f->sidx2, dim, 1, x, &nids);
        ids = malloc(nids*sizeof(int));
        libsupermesh_query_rtree_intersections(f->sidx2, ids);


        if (err != RT_None) {
            fputs("ERROR: Index_Intersects_id failed in libspatialindex!", stderr);
            return -1;
        }
        if (f->extruded == 0) {
            for (uint64_t i = 0; i < nids; i++) {
                int candidate = ids[i]-1;
                current_ref_cell_dist_l1 = (*try_candidate)(temp_ref_coords, f, candidate, x);
                if (current_ref_cell_dist_l1 <= 0.0) {
                    /* Found cell! */
                    cell = candidate;
                    memcpy(found_ref_coords, temp_ref_coords, sizeof(struct ReferenceCoords));
                    found_ref_cell_dist_l1[0] = current_ref_cell_dist_l1;
                    break;
                }
                else if (current_ref_cell_dist_l1 < ref_cell_dist_l1) {
                    /* getting closer... */
                    ref_cell_dist_l1 = current_ref_cell_dist_l1;
                    if (ref_cell_dist_l1 < tolerance) {
                        /* Close to cell within tolerance so could be this cell */
                        cell = candidate;
                        memcpy(found_ref_coords, temp_ref_coords, sizeof(struct ReferenceCoords));
                        found_ref_cell_dist_l1[0] = ref_cell_dist_l1;
                    }
                }
            }
        }
        else {
            for (uint64_t i = 0; i < nids; i++) {
                int nlayers = f->n_layers;
                int c = ids[i] / nlayers;
                int l = ids[i] % nlayers;
                current_ref_cell_dist_l1 = (*try_candidate_xtr)(temp_ref_coords, f, c, l, x);
                if (current_ref_cell_dist_l1 <= 0.0) {
                    /* Found cell! */
                    cell = ids[i];
                    memcpy(found_ref_coords, temp_ref_coords, sizeof(struct ReferenceCoords));
                    found_ref_cell_dist_l1[0] = current_ref_cell_dist_l1;
                    break;
                }
                else if (current_ref_cell_dist_l1 < ref_cell_dist_l1) {
                    /* getting closer... */
                    ref_cell_dist_l1 = current_ref_cell_dist_l1;
                    if (ref_cell_dist_l1 < tolerance) {
                        /* Close to cell within tolerance so could be this cell */
                        cell = ids[i];
                        memcpy(found_ref_coords, temp_ref_coords, sizeof(struct ReferenceCoords));
                        found_ref_cell_dist_l1[0] = ref_cell_dist_l1;
                    }
                }
            }
        }
        free(ids);
    } else {
        if (f->extruded == 0) {
            for (int c = 0; c < f->n_cols; c++) {
                current_ref_cell_dist_l1 = (*try_candidate)(temp_ref_coords, f, c, x);
                if (current_ref_cell_dist_l1 <= 0.0) {
                    /* Found cell! */
                    cell = c;
                    memcpy(found_ref_coords, temp_ref_coords, sizeof(struct ReferenceCoords));
                    found_ref_cell_dist_l1[0] = current_ref_cell_dist_l1;
                    break;
                }
                else if (current_ref_cell_dist_l1 < ref_cell_dist_l1) {
                    /* getting closer... */
                    ref_cell_dist_l1 = current_ref_cell_dist_l1;
                    if (ref_cell_dist_l1 < tolerance) {
                        /* Close to cell within tolerance so could be this cell */
                        cell = c;
                        memcpy(found_ref_coords, temp_ref_coords, sizeof(struct ReferenceCoords));
                        found_ref_cell_dist_l1[0] = ref_cell_dist_l1;
                    }
                }
            }
        }
        else {
            for (int c = 0; c < f->n_cols; c++) {
                for (int l = 0; l < f->n_layers; l++) {
                    current_ref_cell_dist_l1 = (*try_candidate_xtr)(temp_ref_coords, f, c, l, x);
                    if (current_ref_cell_dist_l1 <= 0.0) {
                        /* Found cell! */
                        cell = l;
                        memcpy(found_ref_coords, temp_ref_coords, sizeof(struct ReferenceCoords));
                        found_ref_cell_dist_l1[0] = current_ref_cell_dist_l1;
                        break;
                    }
                    else if (current_ref_cell_dist_l1 < ref_cell_dist_l1) {
                        /* getting closer... */
                        ref_cell_dist_l1 = current_ref_cell_dist_l1;
                        if (ref_cell_dist_l1 < tolerance) {
                            /* Close to cell within tolerance so could be this cell */
                            cell = l;
                            memcpy(found_ref_coords, temp_ref_coords, sizeof(struct ReferenceCoords));
                            found_ref_cell_dist_l1[0] = ref_cell_dist_l1;
                        }
                    }
                }
                if (cell != -1) {
                    cell = c * f->n_layers + cell;
                    break;
                }
            }
        }
    }
    return cell;
}
