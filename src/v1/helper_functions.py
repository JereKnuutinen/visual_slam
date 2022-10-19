import numpy as np
import cv2
import sys

# Matches and normalizes keypoints in 2 frames, needed in many estimations to prevent numerical instability
def MatchAndNormalize(kp1, kp2, matches, K):
    # match keypoints
    pts1 = []
    pts2 = []
    for i,(m) in enumerate(matches):
        #print(m.distance)
        pts2.append(kp2[m[0].trainIdx].pt)
        pts1.append(kp1[m[0].queryIdx].pt)
    pts1  = np.asarray(pts1)
    pts2 = np.asarray(pts2)
    # normalize points
    pts_l_norm = cv2.undistortPoints(np.expand_dims(pts1, axis=1), cameraMatrix=K, distCoeffs=None)
    pts_r_norm = cv2.undistortPoints(np.expand_dims(pts2, axis=1), cameraMatrix=K, distCoeffs=None)
    return pts_l_norm, pts_r_norm

# used in transformation score calculation
def matlab_max(v, s):
        return [max(v[i],s) for i in range(len(v))]

# Expects pts1 and pts2 to be matched and normalized with intrinsics
def estimateEssential(pts1, pts2, essTh):
    #E, inliers = cv2.findEssentialMat(pts1, pts2, focal=1.0, pp=(0., 0.), method=cv2.RANSAC, prob=0.999, threshold=3.0/essTh) # threshold=3.0 / essTh
    E, inliers = cv2.findEssentialMat(pts1, pts2, method=cv2.LMEDS) # threshold=3.0 / essTh
    # https://docs.opencv.org/4.x/da/de9/tutorial_py_epipolar_geometry.html
    inlierPoints1 = pts1[inliers==1]
    inlierPoints2 = pts2[inliers==1]
    
    
    lineIn1 = cv2.computeCorrespondEpilines(inlierPoints2.reshape(-1,1,2), 2,E) # original with F
    lineIn1 = lineIn1.reshape(-1,3)
    

    inliersIndex  = np.where(inliers==1)

    locations1 = (np.concatenate(    (inlierPoints1, np.ones((np.shape(inlierPoints1)[0], 1)))    , axis=1))
    locations2 = (np.concatenate(    (inlierPoints2, np.ones((np.shape(inlierPoints2)[0], 1)))   , axis=1))
    
    error2in1 = (np.sum(locations1 * lineIn1, axis = 1))**2 / np.sum(lineIn1[:,:3]**2, axis=1)
    
    lineIn2 = cv2.computeCorrespondEpilines(inlierPoints1.reshape(-1,1,2), 2,E) # original with F
    lineIn2 = lineIn2.reshape(-1,3)
    
    error1in2 = (np.sum(locations2 * lineIn2, axis = 1))**2 / np.sum(lineIn2[:,:3]**2, axis=1)
    
    
    outlierThreshold = 4

    

    score = np.sum(matlab_max(outlierThreshold-error1in2, 0)) + sum(matlab_max(outlierThreshold-error2in1, 0))



    return E, inliers, score
        
# Expects pts1 and pts2 to be matched and normalized with intrinsics
def estimateHomography(pts1, pts2, homTh):
    #H, inliers = cv2.findHomography(pts1, pts2, cv2.RANSAC, ransacReprojThreshold=3.0/homTh)
    H, inliers = cv2.findHomography(pts1, pts2, cv2.LMEDS)

    inlierPoints1 = pts1[inliers==1]
    inlierPoints2 = pts2[inliers==1]

    inliersIndex  = np.where(inliers==1)

    locations1 = (np.concatenate(    (inlierPoints1, np.ones((np.shape(inlierPoints1)[0], 1)))    , axis=1))
    locations2 = (np.concatenate(    (inlierPoints2, np.ones((np.shape(inlierPoints2)[0], 1)))   , axis=1))
    xy1In2     = (H @ locations1.T).T
    xy2In1     = (np.linalg.inv(H) @ locations2.T).T
    error1in2  = np.sum((locations2 - xy1In2)**2, axis=1)
    error2in1  = np.sum((locations1 - xy2In1)**2, axis=1)

    outlierThreshold = 6

    score = np.sum(matlab_max(outlierThreshold-error1in2, 0)) + np.sum(matlab_max(outlierThreshold-error2in1, 0))

    return H, inliers, score

def triangulateMidPoint(points1, points2, P1, P2):
    points1 = np.squeeze(points1)
    points2 = np.squeeze(points2)
    numPoints = np.shape(points1)[0]
    points3D = np.zeros((numPoints,3))
    P1 = P1.T
    P2 = P2.T
    M1 = P1[:3, :3]
    M2 = P2[:3, :3]
    # Get least-squares solution
    c1 = np.linalg.lstsq(-M1,  P1[:,3], rcond=None)[0]
    c2 = np.linalg.lstsq(-M2, P2[:,3], rcond=None)[0]
    y = c2 - c1
    u1 = np.concatenate((points1, np.ones((numPoints,1))), axis=1)
    u2 = np.concatenate((points2, np.ones((numPoints,1))), axis=1)
    #u1 = [points1, ones(numPoints, 1, 'like', points1)]'
    #u2 = [points2, ones(numPoints, 1, 'like', points1)]'
    a1 = np.linalg.lstsq(M1, u1.T, rcond=None)[0]
    a2 = np.linalg.lstsq(M2, u2.T, rcond=None)[0]
    #isCodegen  = ~isempty(coder.target);
    condThresh = 2**(-52)
    for i in range(numPoints):
        A   = np.array([a1[:,i], -a2[:,i]]).T 
        AtA = A.T@A
        if np.linalg.cond(AtA) < condThresh: # original: rcond(AtA) < condThresh
            # Guard against matrix being singular or ill-conditioned
            p    = np.inf(3, 1)
            p[2] = -p[2]
        else:
            alpha = np.linalg.lstsq(A, y, rcond=None)[0]
            p = (c1 + (alpha[0] * a1[:,i]).T + c2 + (alpha[1] * a2[:,i]).T) / 2
            
        points3D[i, :] = p.T

    return points3D

def chooseRealizableSolution(Rs, Ts, K, points1, points2):
    # Rs is 4x3x3, holding all possible solutions of Rotation matrix
    # Ts is 4x3x1, holding all possible solutions of Translation vector
    numNegatives = np.zeros((np.shape(Ts)[0], 1))
    #  The camera matrix is computed as follows:
    #  camMatrix = [rotationMatrix; translationVector] * K
    #  where K is the intrinsic matrix.
    #camMatrix1 = cameraMatrix(cameraParams1, np.eye(3), np.array([0 0 0]));
    camMatrix1 = np.concatenate((np.eye(3), np.zeros((1,3))), axis=0) @ K
    
    for i in range(np.shape(Ts)[0]):
        #camMatrix2 = cameraMatrix(cameraParams2, Rs(:,:,i)', Ts(i, :));
        #camMatrix2 is 4x3 @ 3x3 matmul
        camMatrix2 = np.concatenate((Rs[i].T, Ts[i].T),axis=0) @ K
        m1 = triangulateMidPoint(points1, points2, camMatrix1, camMatrix2)
        #m2 = bsxfun(@plus, m1 * Rs(:,:,i)', Ts(i, :));
        m2 = (m1 @ (Rs[i]).T) + Ts[i].T
        numNegatives[i] = np.sum((m1[:,2] < 0) | (m2[:,2] < 0))
        
    val = np.min(numNegatives)
    idx = np.where(numNegatives==val)
    validFraction = 1 - (val / points1.shape[0])
    
    R = np.zeros((len(idx), 3,3))
    t = np.zeros((len(idx), 3))
    for n in range(len(idx)):
        idx_n = idx[n][0]
        R0 = Rs[idx_n].T
        t0 = Ts[idx_n].T

        tNorm = np.linalg.norm(t0)
        if tNorm != 0:
            t0 = t0 / tNorm
        R[n] = R0
        t[n] = t0

    return R, t, validFraction


def estimateRelativePose(tform, inlier_pts1, inlier_pts2, K, tform_type = "Essential"):
    if tform_type == "Homography":
        # decompose homography into 4 possible solutions
        num, Rs, Ts, Ns  = cv2.decomposeHomographyMat(tform, K)
        # choose realizable solutions according to cheirality check
        R, t, validFraction = chooseRealizableSolution(Rs, Ts, K, inlier_pts1, inlier_pts2)
        if np.shape(R)[0] >= 2:
            return R[1], t[1], validFraction
        else:
            return R, t, validFraction
        
    elif tform_type == "Essential":
        # recoverpose way:
        #points, R, t, inliers = cv2.recoverPose(tform, inlier_pts1, inlier_pts2, cameraMatrix=K)
        #validFraction = np.sum(inliers) / len(inliers)
        #return R, t, validFraction 
        # decompose essential matrix into 4 possible solutions
        R1, R2, t = cv2.decomposeEssentialMat(tform)
        # The possible solutions are (R1,t), (R1,-t), (R2,t), (R2,-t)
        R1, R2, t = R1[np.newaxis,:], R2[np.newaxis,:], t[np.newaxis,:]
        Rs = np.concatenate((R1, R1, R2, R2), axis=0)
        Ts = np.concatenate((t,-t,t,-t))
        # choose realizable solutions according to cheirality check
        R, t, validFraction = chooseRealizableSolution(Rs, Ts, K, inlier_pts1, inlier_pts2)
        if np.shape(R)[0] >= 2:
            return R[1], t[1], validFraction
        else:
            return R, t, validFraction
    else:
        print("Unknown tform_type")
        return None, None, 0
        